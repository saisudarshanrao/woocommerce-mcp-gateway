"""The provisioner against a pretend cluster: creating, failing, resuming, deleting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from gateway import repo
from gateway.provisioner.helm import HelmError
from gateway.provisioner.kube import KubeError
from gateway.provisioner.provisioner import MANAGED_LABEL, Provisioner, ProvisionerSettings, waiting_on

from .conftest import CIPHER, add_user

pytestmark = pytest.mark.anyio

SETTINGS = ProvisionerSettings(
    values_files=("/charts/store/values-local.yaml",), host_template="{slug}.shops.test", poll_seconds=0.01,
    timeout_seconds=30,
)


def workload(name: str, ready: bool) -> dict:
    return {
        "metadata": {"name": name, "generation": 1},
        "spec": {"replicas": 1},
        "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1 if ready else 0},
    }


class FakeCluster:
    """Stands in for both the Kubernetes API and helm, and remembers what was asked of it."""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict] = {}
        self.releases: dict[str, str] = {}  # namespace -> helm status
        self.installs: list[tuple[str, dict]] = []
        self.server_installs: list[tuple] = []
        self.certificate_issued = False
        self.recorded_warnings: dict[str, str] = {}
        self.bindings: list[tuple[str, str, str]] = []
        self.ready: set[str] = set()  # namespaces whose pods have come up
        self.becomes_ready_after_checks = 2
        self.helm_error: str | None = None
        self.kube_errors: list[KubeError] = []
        self._checks: dict[str, int] = {}

    # Kubernetes
    async def get_namespace(self, name):
        if self.kube_errors:
            raise self.kube_errors.pop(0)
        namespace = self.namespaces.get(name)
        if namespace and namespace["status"]["phase"] == "Terminating":
            # Each look at a namespace being deleted brings it one step closer to gone.
            namespace["ticks"] -= 1
            if namespace["ticks"] <= 0:
                del self.namespaces[name]
                self.releases.pop(name, None)
                return None
        return namespace

    async def create_namespace(self, name, labels):
        assert name not in self.namespaces
        self.namespaces[name] = {"metadata": {"name": name, "labels": labels}, "status": {"phase": "Active"}}

    async def delete_namespace(self, name):
        self.namespaces[name]["status"]["phase"] = "Terminating"
        self.namespaces[name]["ticks"] = 2

    async def bind_cluster_role(self, namespace, name, cluster_role, account, account_namespace):
        self.bindings.append((namespace, cluster_role, f"{account_namespace}/{account}"))

    async def read_secret(self, namespace, name):
        if name == "store-tls":
            return {"tls.crt": "certificate"} if self.certificate_issued else None
        return {"mcp-auth-token": f"token-of-{namespace}"} if namespace in self.releases else None

    async def workloads(self, namespace):
        if namespace not in self.releases:
            return []
        self._checks[namespace] = self._checks.get(namespace, 0) + 1
        if self._checks[namespace] > self.becomes_ready_after_checks:
            self.ready.add(namespace)
        up = namespace in self.ready
        if namespace.startswith("mcp-"):
            return [workload("server-mcp", up)]
        return [workload("store-wordpress", up), workload("store-mariadb", True), workload("store-woo-mcp", up)]

    async def pods(self, namespace):
        if namespace.startswith("mcp-"):
            return [{
                "metadata": {"name": "server-mcp-xyz"},
                "status": {"containerStatuses": [{"name": "server", "state": {"waiting": {"reason": "ImagePullBackOff"}}}]},
            }]
        return [{
            "metadata": {"name": "store-wordpress-abc"},
            "status": {"initContainerStatuses": [{"name": "setup", "state": {"waiting": {"reason": "ImagePullBackOff"}}}]},
        }]

    async def warnings(self, namespace):
        return self.recorded_warnings

    # helm
    async def status(self, namespace, release):
        return self.releases.get(namespace)

    async def install(self, namespace, release, chart, values_files, values, secret_values=None):
        if self.helm_error:
            raise HelmError(self.helm_error)
        self.installs.append((namespace, dict(values)))
        if secret_values is not None:
            self.server_installs.append((namespace, release, chart, dict(values), secret_values))
        self.releases[namespace] = "deployed"

    async def uninstall(self, namespace, release):
        self.releases.pop(namespace, None)


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
async def provisioner(pool, cluster):
    provisioner = Provisioner(pool, CIPHER, cluster, cluster, SETTINGS)
    yield provisioner
    await provisioner.stop()


@pytest.fixture
async def owner(pool):
    return await add_user(pool, "owner@example.com")


async def settle(provisioner: Provisioner, rounds: int = 3) -> None:
    """Runs the loop by hand until the work it started is finished."""
    for _ in range(rounds):
        await provisioner.tick()
        await provisioner.idle()


async def new_store(pool, owner, slug="books", name="Books") -> repo.Store:
    return await repo.create_store(pool, slug=slug, name=name, owner=owner, limit=10)


async def test_a_requested_store_is_installed_in_its_own_namespace_and_registered(pool, cluster, provisioner, owner):
    store = await new_store(pool, owner)
    await settle(provisioner)

    ready = await repo.get_store(pool, store.id)
    assert (ready.status, ready.url, ready.detail) == ("ready", "http://books.shops.test", None)

    labels = cluster.namespaces["store-books"]["metadata"]["labels"]
    assert labels[MANAGED_LABEL] == "books" and labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert cluster.bindings == [("store-books", "gateway-store-installer", "gateway/gateway-provisioner")]
    assert cluster.installs == [
        ("store-books", {
            "store.host": "books.shops.test", "store.title": "Books", "store.adminEmail": owner.email,
            "networkPolicy.gateway.namespace": "gateway",
        })
    ]

    server = await repo.get_server_by_slug(pool, "store-books")
    assert server.store_id == store.id and server.name == "Books store" and server.auth_type == "bearer"
    assert server.url == "http://store-woo-mcp.store-books.svc.cluster.local/mcp"
    # The token is stored encrypted, like any other upstream credential.
    assert b"token-of-store-books" not in server.auth_secret
    assert CIPHER.decrypt(server.auth_secret) == "token-of-store-books"


async def test_several_stores_are_provisioned_at_the_same_time(pool, cluster, provisioner, owner):
    cluster.becomes_ready_after_checks = 10_000  # never, until the test says so
    for slug in ("one", "two", "three"):
        await new_store(pool, owner, slug, slug.title())
    await provisioner.tick()
    await asyncio.sleep(0.2)

    # All three are installed and waiting on their pods, none held up by another.
    assert sorted(namespace for namespace, _ in cluster.installs) == ["store-one", "store-three", "store-two"]
    assert {store.status for store in await repo.list_stores(pool)} == {"provisioning"}
    detail = (await repo.get_store_by_slug(pool, "two")).detail
    assert detail == "Waiting for store-wordpress (ImagePullBackOff), store-woo-mcp (starting)"

    cluster.ready |= {"store-one", "store-two", "store-three"}
    await provisioner.idle()
    assert {store.status for store in await repo.list_stores(pool)} == {"ready"}


async def test_a_store_that_never_comes_up_fails_at_the_timeout_and_says_what_it_was_waiting_for(
    pool, cluster, provisioner, owner
):
    cluster.becomes_ready_after_checks = 10_000
    store = await new_store(pool, owner)
    await pool.execute("UPDATE stores SET started_at = $2 WHERE id = $1", store.id, datetime.now(UTC) - timedelta(seconds=29.9))
    await settle(provisioner)

    failed = await repo.get_store(pool, store.id)
    assert failed.status == "failed"
    assert failed.detail == (
        "Gave up after 0.5 minutes. Last seen: Waiting for store-wordpress (ImagePullBackOff), store-woo-mcp (starting)"
    )
    assert await repo.get_server_by_slug(pool, "store-books") is None


async def test_a_helm_error_fails_the_store_with_helms_own_words_and_retry_starts_again(pool, cluster, provisioner, owner):
    cluster.helm_error = "Error: values don't meet the specifications of the schema"
    store = await new_store(pool, owner)
    await settle(provisioner)
    failed = await repo.get_store(pool, store.id)
    assert failed.status == "failed" and "values don't meet" in failed.detail

    # A failed store is left alone until someone asks for another attempt.
    await settle(provisioner)
    assert cluster.installs == []

    cluster.helm_error = None
    assert (await repo.retry_store(pool, store.id)).status == "provisioning"
    await settle(provisioner)
    assert (await repo.get_store(pool, store.id)).status == "ready"
    assert len(cluster.namespaces) == 1


async def test_a_restart_halfway_through_resumes_without_doing_anything_twice(pool, cluster, owner):
    cluster.becomes_ready_after_checks = 10_000
    store = await new_store(pool, owner)
    first = Provisioner(pool, CIPHER, cluster, cluster, SETTINGS)
    await first.tick()
    await asyncio.sleep(0.1)
    await first.stop()  # the process dies while waiting for the pods
    assert (await repo.get_store(pool, store.id)).status == "provisioning"

    cluster.ready.add("store-books")
    second = Provisioner(pool, CIPHER, cluster, cluster, SETTINGS)
    await settle(second)
    assert (await repo.get_store(pool, store.id)).status == "ready"
    # The namespace and the release were found as they were, not created again.
    assert len(cluster.installs) == 1
    assert len(await repo.list_servers(pool)) == 1


async def test_an_install_cut_off_by_a_restart_is_cleared_and_run_again(pool, cluster, provisioner, owner):
    store = await new_store(pool, owner)
    await cluster.create_namespace("store-books", {MANAGED_LABEL: "books"})
    cluster.releases["store-books"] = "pending-install"
    await settle(provisioner)
    assert (await repo.get_store(pool, store.id)).status == "ready"
    assert len(cluster.installs) == 1


async def test_a_namespace_that_is_not_the_provisioners_is_never_taken_over_or_deleted(pool, cluster, provisioner, owner):
    cluster.namespaces["store-main"] = {"metadata": {"name": "store-main", "labels": {}}, "status": {"phase": "Active"}}
    store = await new_store(pool, owner, "main", "Main")
    await settle(provisioner)
    failed = await repo.get_store(pool, store.id)
    assert failed.status == "failed" and "already exists" in failed.detail
    assert cluster.installs == []

    await repo.mark_store_deleting(pool, store.id)
    await settle(provisioner)
    assert await repo.get_store(pool, store.id) is None
    assert cluster.namespaces["store-main"]["status"]["phase"] == "Active"


async def test_a_refusal_from_kubernetes_fails_at_once_and_a_passing_error_is_retried(pool, cluster, provisioner, owner):
    cluster.kube_errors = [KubeError(503, "the server is currently unable to handle the request")]
    store = await new_store(pool, owner)
    await provisioner.tick()
    await provisioner.idle()
    hiccup = await repo.get_store(pool, store.id)
    assert hiccup.status == "provisioning" and "Trying again after an error" in hiccup.detail
    await settle(provisioner)
    assert (await repo.get_store(pool, store.id)).status == "ready"

    cluster.kube_errors = [KubeError(403, "namespaces is forbidden")]
    refused = await new_store(pool, owner, "toys", "Toys")
    await settle(provisioner)
    failed = await repo.get_store(pool, refused.id)
    assert (failed.status, failed.detail) == ("failed", "Kubernetes refused: namespaces is forbidden")


async def test_deleting_a_store_removes_its_tools_at_once_and_then_everything_else(pool, cluster, provisioner, owner):
    store = await new_store(pool, owner)
    await settle(provisioner)
    assert await repo.get_server_by_slug(pool, "store-books")

    await repo.mark_store_deleting(pool, store.id)
    # Before the provisioner has done anything, the gateway no longer has the server.
    assert await repo.get_server_by_slug(pool, "store-books") is None
    assert (await repo.get_store(pool, store.id)).status == "deleting"

    await settle(provisioner)
    assert await repo.get_store(pool, store.id) is None
    assert "store-books" not in cluster.namespaces


async def test_deleting_a_store_while_it_is_being_created_stops_the_creation(pool, cluster, provisioner, owner):
    cluster.becomes_ready_after_checks = 10_000
    store = await new_store(pool, owner)
    await provisioner.tick()
    await asyncio.sleep(0.1)
    await repo.mark_store_deleting(pool, store.id)
    await settle(provisioner)
    assert await repo.get_store(pool, store.id) is None
    assert cluster.namespaces == {} and await repo.list_servers(pool) == []


async def test_a_store_can_be_created_again_under_a_name_that_is_still_being_removed(pool, cluster, provisioner, owner):
    first = await new_store(pool, owner)
    await settle(provisioner)
    await repo.mark_store_deleting(pool, first.id)
    await provisioner.tick()
    await asyncio.sleep(0.005)  # the namespace is now Terminating
    await provisioner.stop()
    await pool.execute("DELETE FROM stores WHERE id = $1", first.id)

    again = await new_store(pool, owner)
    await settle(provisioner, rounds=6)
    assert (await repo.get_store(pool, again.id)).status == "ready"
    assert len(cluster.installs) == 2


async def test_the_identifier_of_a_server_someone_else_registered_is_not_overwritten(pool, cluster, provisioner, owner):
    from .conftest import add_server

    await add_server(pool, "store-books", "elsewhere.test")
    store = await new_store(pool, owner)
    await settle(provisioner)
    failed = await repo.get_store(pool, store.id)
    assert failed.status == "failed" and "already has a server" in failed.detail
    assert (await repo.get_server_by_slug(pool, "store-books")).url == "http://elsewhere.test/mcp"


async def test_the_limit_on_stores_per_person_holds_when_requests_arrive_together(pool, owner):
    made = await asyncio.gather(
        *(repo.create_store(pool, slug=f"s{n}", name=f"S{n}", owner=owner, limit=3) for n in range(8))
    )
    assert sum(store is not None for store in made) == 3


def test_what_is_not_ready_is_described_from_the_workloads_and_their_pods():
    stuck = workload("store-wordpress", False)
    quota = workload("store-mariadb", False)
    quota["status"]["conditions"] = [{"type": "ReplicaFailure", "message": "exceeded quota: store-quota"}]
    pods = [{
        "metadata": {"name": "store-wordpress-7d9"},
        "status": {"conditions": [{"type": "PodScheduled", "status": "False", "message": "0/1 nodes: Insufficient memory"}]},
    }]
    assert waiting_on([stuck, quota, workload("store-woo-mcp", True)], pods) == [
        "store-wordpress (cannot be scheduled: 0/1 nodes: Insufficient memory)",
        "store-mariadb (exceeded quota: store-quota)",
    ]


async def test_only_one_provisioner_works_at_a_time(pool, cluster, owner, tmp_path):
    from dataclasses import replace

    await new_store(pool, owner)
    beat = tmp_path / "alive"
    first = Provisioner(pool, CIPHER, cluster, cluster, replace(SETTINGS, heartbeat_file=str(beat)))
    standby_cluster = FakeCluster()
    second = Provisioner(pool, CIPHER, standby_cluster, standby_cluster, SETTINGS)
    running = asyncio.create_task(first.run())
    await asyncio.sleep(0.3)
    waiting = asyncio.create_task(second.run())
    await asyncio.sleep(0.3)

    assert (await repo.get_store_by_slug(pool, "books")).status == "ready" and beat.exists()
    assert standby_cluster.namespaces == {} and standby_cluster.installs == []

    # When the first one goes, the one standing by takes over.
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    await new_store(pool, owner, "toys", "Toys")
    await asyncio.sleep(0.5)
    assert "store-toys" in standby_cluster.namespaces
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


# --- hosted servers: an image instead of a URL -------------------------------------


async def new_hosted(pool, slug="notes", **spec) -> repo.Server:
    from gateway.hosted import HostedSpec, url

    described = HostedSpec(image="registry.test/notes-mcp:1.4", args=("serve",), env={"LOG_LEVEL": "debug"}, **spec)
    return await repo.create_server(
        pool, created_by=None, slug=slug, name="Notes", url=url(slug, described, "cluster.local"),
        auth_type="bearer", auth_secret=CIPHER.encrypt("made-up-by-the-gateway"), hosted=described.to_json(),
    )


async def test_an_image_is_deployed_into_its_own_namespace_with_the_gateways_credential(pool, cluster, provisioner):
    server = await new_hosted(pool, internet=True)
    assert (server.hosted_status, server.url) == ("deploying", "http://server-mcp.mcp-notes.svc.cluster.local/mcp")
    await settle(provisioner)

    running = await repo.get_server(pool, server.id)
    assert (running.hosted_status, running.hosted_detail) == ("running", None)
    # Finishing counts as a change, so every gateway replica checks the server afresh.
    assert running.updated_at > server.updated_at

    labels = cluster.namespaces["mcp-notes"]["metadata"]["labels"]
    assert labels == {"mcp-gateway/hosted": "notes", "pod-security.kubernetes.io/enforce": "restricted"}
    assert cluster.bindings == [("mcp-notes", "gateway-store-installer", "gateway/gateway-provisioner")]

    ((namespace, release, chart, on_command_line, given_privately),) = cluster.server_installs
    assert (namespace, release, chart) == ("mcp-notes", "server", "/charts/mcp-server")
    # The credential reaches helm on standard input only, never as an argument.
    assert on_command_line == {}
    assert given_privately["auth"] == {"enabled": True, "header": "Authorization", "token": "made-up-by-the-gateway"}
    assert given_privately["image"] == {"reference": "registry.test/notes-mcp:1.4"}
    assert given_privately["args"] == ["serve"] and given_privately["env"] == {"LOG_LEVEL": "debug"}
    assert given_privately["networkPolicy"] == {"gateway": {"namespace": "gateway"}, "egress": {"internet": True}}


async def test_an_image_that_never_starts_says_why_and_can_be_removed_meanwhile(pool, cluster, provisioner):
    cluster.becomes_ready_after_checks = 10_000
    server = await new_hosted(pool)
    await provisioner.tick()
    await asyncio.sleep(0.1)
    stuck = await repo.get_server(pool, server.id)
    assert (stuck.hosted_status, stuck.hosted_detail) == ("deploying", "Waiting for server-mcp (ImagePullBackOff)")

    removing = await repo.mark_hosted_deleting(pool, server.id)
    # Disabled in the same statement: the gateway stops offering it before anything is torn down.
    assert (removing.hosted_status, removing.enabled) == ("deleting", False)
    await settle(provisioner)
    assert await repo.get_server(pool, server.id) is None
    assert cluster.namespaces == {}


async def test_a_deployment_that_times_out_fails_and_can_be_tried_again(pool, cluster, provisioner):
    cluster.becomes_ready_after_checks = 10_000
    server = await new_hosted(pool)
    await pool.execute(
        "UPDATE upstream_servers SET hosted_started_at = $2 WHERE id = $1", server.id, datetime.now(UTC) - timedelta(seconds=29.9)
    )
    await settle(provisioner)
    failed = await repo.get_server(pool, server.id)
    assert failed.hosted_status == "failed"
    assert failed.hosted_detail == "Gave up after 0.5 minutes. Last seen: Waiting for server-mcp (ImagePullBackOff)"

    cluster.ready.add("mcp-notes")
    assert (await repo.redeploy_hosted(pool, server.id)).hosted_status == "deploying"
    await settle(provisioner)
    assert (await repo.get_server(pool, server.id)).hosted_status == "running"
    assert len(cluster.namespaces) == 1


async def test_changing_the_image_installs_again_and_an_older_attempt_steps_aside(pool, cluster, provisioner):
    from gateway.hosted import HostedSpec

    cluster.becomes_ready_after_checks = 10_000
    server = await new_hosted(pool)
    await provisioner.tick()
    await asyncio.sleep(0.1)

    newer = HostedSpec(image="registry.test/notes-mcp:1.5")
    await repo.redeploy_hosted(pool, server.id, newer.to_json())
    cluster.ready.add("mcp-notes")
    await settle(provisioner)

    assert (await repo.get_server(pool, server.id)).hosted_status == "running"
    assert [install[4]["image"]["reference"] for install in cluster.server_installs] == [
        "registry.test/notes-mcp:1.4", "registry.test/notes-mcp:1.5",
    ]


async def test_a_hosted_server_never_takes_over_a_namespace_that_is_not_its_own(pool, cluster, provisioner):
    cluster.namespaces["mcp-servers"] = {"metadata": {"name": "mcp-servers", "labels": {}}, "status": {"phase": "Active"}}
    server = await new_hosted(pool, slug="servers")
    await settle(provisioner)
    failed = await repo.get_server(pool, server.id)
    assert failed.hosted_status == "failed" and "already exists" in failed.hosted_detail
    assert cluster.server_installs == []

    await repo.mark_hosted_deleting(pool, server.id)
    await settle(provisioner)
    assert await repo.get_server(pool, server.id) is None
    assert cluster.namespaces["mcp-servers"]["status"]["phase"] == "Active"


async def test_stores_and_hosted_servers_are_worked_on_side_by_side(pool, cluster, provisioner, owner):
    store = await new_store(pool, owner)
    server = await new_hosted(pool)
    await settle(provisioner)
    assert (await repo.get_store(pool, store.id)).status == "ready"
    assert (await repo.get_server(pool, server.id)).hosted_status == "running"
    assert set(cluster.namespaces) == {"store-books", "mcp-notes"}
    # A server registered by URL is none of the provisioner's business.
    assert {s.slug for s in await repo.list_hosted_work(pool)} == set()


def test_a_container_kubernetes_refuses_to_start_is_reported_with_the_reason():
    refused = workload("server-mcp", False)
    pods = [{
        "metadata": {"name": "server-mcp-5d"},
        "status": {"containerStatuses": [{"name": "server", "state": {"waiting": {
            "reason": "CreateContainerConfigError", "message": "container has runAsNonRoot and image will run as root",
        }}}]},
    }]
    assert waiting_on([refused], pods) == [
        "server-mcp (CreateContainerConfigError: container has runAsNonRoot and image will run as root)"
    ]


async def test_with_tls_a_store_is_not_ready_until_its_certificate_has_been_issued(pool, cluster, owner):
    from dataclasses import replace

    provisioner = Provisioner(pool, CIPHER, cluster, cluster, replace(SETTINGS, url_scheme="https"))
    store = await new_store(pool, owner)
    await provisioner.tick()
    await asyncio.sleep(0.2)
    # The pods are up, but nothing is registered until the certificate exists.
    waiting = await repo.get_store(pool, store.id)
    assert (waiting.status, waiting.detail) == ("provisioning", "Waiting for the store's TLS certificate")
    assert await repo.list_servers(pool) == []

    cluster.certificate_issued = True
    await provisioner.idle()
    ready = await repo.get_store(pool, store.id)
    assert (ready.status, ready.url) == ("ready", "https://books.shops.test")
    await provisioner.stop()


def test_a_workload_with_no_pod_at_all_is_explained_by_what_kubernetes_recorded_about_it():
    # A StatefulSet whose volume a quota refused: no pod exists to ask, only an event.
    database = workload("store-mariadb", False)
    warnings = {"store-mariadb": "create Claim data-store-mariadb-0 failed: exceeded quota: store-quota"}
    assert waiting_on([database], [], warnings) == [
        "store-mariadb (create Claim data-store-mariadb-0 failed: exceeded quota: store-quota)"
    ]
    # A pod's own state, when there is one, is the more direct answer.
    pods = [{"metadata": {"name": "store-mariadb-0"}, "status": {"containerStatuses": [
        {"name": "mariadb", "state": {"waiting": {"reason": "ImagePullBackOff"}}}]}}]
    assert waiting_on([database], pods, warnings) == ["store-mariadb (ImagePullBackOff)"]


def test_a_changed_workload_is_not_ready_while_a_pod_of_the_old_version_remains():
    rolling = workload("server-mcp", True)
    # The new pod is not up yet; the one that is ready is the old version.
    rolling["status"] |= {"replicas": 2, "updatedReplicas": 1, "readyReplicas": 1}
    assert waiting_on([rolling], []) == ["server-mcp (starting)"]
    # New pod ready, old one still being stopped.
    rolling["status"] |= {"replicas": 2, "updatedReplicas": 1, "readyReplicas": 2}
    assert waiting_on([rolling], []) == ["server-mcp (starting)"]
    rolling["status"] |= {"replicas": 1, "updatedReplicas": 1, "readyReplicas": 1}
    assert waiting_on([rolling], []) == []
