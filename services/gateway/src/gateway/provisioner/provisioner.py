"""Makes the cluster match what the database says should exist.

The API only ever writes a row: "this store should exist", "this server should
run", or "this should go". This loop reads those rows and does the work, one
task per row, several at a time. Two kinds of thing are provisioned, each an
install of a Helm chart into a namespace of its own:

    a store           the stores table            chart store        store-<slug>
    a hosted server   upstream_servers.hosted     chart mcp-server   mcp-<slug>

Both go through the same steps:

    creating  ->  namespace, permissions, helm install, wait for the pods  ->  done
    deleting  ->  delete the namespace, wait until it is gone, drop the row

Every step first looks at what already exists and only does what is missing.
That one property is what makes the rest safe: pressing "create" twice, retrying
something that failed, and this process being restarted halfway through all
come down to running the same steps again from the top.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from gateway import hosted, repo
from gateway.provisioner.helm import HelmError
from gateway.provisioner.kube import KubeError
from gateway.repo import Server, Store
from gateway.security import CredentialCipher

logger = logging.getLogger(__name__)

# Only one provisioner works at a time, whichever holds this Postgres lock.
LEADER_LOCK_ID = 7_265_001
# The label that marks a namespace as created for a store. Hosted servers have
# their own (hosted.LABEL). The cluster's admission policy lets the provisioner
# touch only namespaces that carry one of the two.
MANAGED_LABEL = "mcp-gateway/store"
RELEASE = "store"

Say = Callable[[str], Awaitable[None]]


class KubeApi(Protocol):
    async def get_namespace(self, name: str) -> dict | None: ...
    async def create_namespace(self, name: str, labels: dict[str, str]) -> None: ...
    async def delete_namespace(self, name: str) -> None: ...
    async def bind_cluster_role(self, namespace: str, name: str, cluster_role: str, account: str, account_namespace: str) -> None: ...
    async def read_secret(self, namespace: str, name: str) -> dict[str, str] | None: ...
    async def workloads(self, namespace: str) -> list[dict]: ...
    async def pods(self, namespace: str) -> list[dict]: ...
    async def warnings(self, namespace: str) -> dict[str, str]: ...


class HelmCli(Protocol):
    async def status(self, namespace: str, release: str) -> str | None: ...
    async def install(
        self, namespace: str, release: str, chart: str, values_files: list[str], values: dict[str, str],
        secret_values: dict[str, Any] | None = None,
    ) -> None: ...
    async def uninstall(self, namespace: str, release: str) -> None: ...


@dataclass(frozen=True)
class ProvisionerSettings:
    chart: str = "/charts/store"
    # Values files applied to every store, in order: the environment's file
    # from the chart itself, then the deployment's overrides.
    values_files: tuple[str, ...] = ()
    # The chart a hosted MCP server is installed from.
    server_chart: str = "/charts/mcp-server"
    # The public hostname of a store, with {slug} standing for its identifier.
    host_template: str = "{slug}.localhost"
    url_scheme: str = "http"
    cluster_domain: str = "cluster.local"
    # The ClusterRole holding what an install needs, and who to grant it to.
    installer_role: str = "gateway-store-installer"
    service_account: str = "gateway-provisioner"
    service_account_namespace: str = "gateway"
    timeout_seconds: float = 600
    max_concurrent: int = 3
    poll_seconds: float = 2.0
    # Touched on every round of the loop. The pod's health checks look at its age.
    heartbeat_file: str = ""


class Failed(Exception):
    """Provisioning cannot succeed as things stand. The message is shown to the user."""


def waiting_on(workloads: list[dict], pods: list[dict], warnings: dict[str, str] | None = None) -> list[str]:
    """What is not ready yet, in words. Empty when every workload has all its pods ready.

    The reason comes from the workload's own status if it gives one, else from
    its pod, else from the latest warning Kubernetes recorded about it.
    """
    waiting = []
    for workload in workloads:
        wanted = workload["spec"].get("replicas", 1)
        status = workload.get("status", {})
        # The same test `kubectl rollout status` applies: Kubernetes has seen
        # the latest version of the workload, every pod it wants is of that
        # version and ready, and no pod of an older version is left. Without
        # the last part a changed server counts as ready while its old pod is
        # still the one answering.
        if (
            status.get("observedGeneration") == workload["metadata"].get("generation")
            and status.get("updatedReplicas", 0) >= wanted
            and status.get("readyReplicas", 0) >= wanted
            and status.get("replicas", 0) == wanted
        ):
            continue
        name = workload["metadata"]["name"]
        reason = next(
            (c.get("message", "") for c in status.get("conditions", []) if c.get("type") == "ReplicaFailure"), ""
        )
        reason = reason or pod_state(name, pods) or (warnings or {}).get(name, "")
        waiting.append(f"{name} ({reason[:240] or 'starting'})")
    return waiting


def pod_state(workload: str, pods: list[dict]) -> str:
    """Why this workload's pod is not ready: the reason Kubernetes gives for its waiting container."""
    for pod in pods:
        if not pod["metadata"]["name"].startswith(f"{workload}-"):
            continue
        status = pod.get("status", {})
        for condition in status.get("conditions", []):
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                return f"cannot be scheduled: {condition.get('message', '')}"[:160]
        for kind, containers in (("init", status.get("initContainerStatuses", [])), ("main", status.get("containerStatuses", []))):
            for container in containers:
                state = container.get("state", {})
                if "waiting" in state and state["waiting"].get("reason") not in (None, "PodInitializing"):
                    waiting = state["waiting"]
                    # "CreateContainerConfigError" alone says little; its message says, for
                    # instance, that the image wants to run as root.
                    message = waiting.get("message", "") if waiting["reason"].endswith("Error") else ""
                    return f"{waiting['reason']}: {message}"[:200] if message else waiting["reason"]
                if "running" in state and kind == "init":
                    return f"running its {container['name']} step"
                if "running" in state and not container.get("ready"):
                    return "started, not answering yet"
    return ""


class Provisioner:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: CredentialCipher,
        kube: KubeApi,
        helm: HelmCli,
        settings: ProvisionerSettings,
    ):
        self._pool = pool
        self._cipher = cipher
        self._kube = kube
        self._helm = helm
        self._settings = settings
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(settings.max_concurrent)

    # --- the loop ------------------------------------------------------------

    async def run(self) -> None:
        """Works until cancelled. Stands by for as long as another provisioner holds the lock."""
        async with self._pool.acquire() as connection:
            while not await connection.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK_ID):
                self._heartbeat()
                await asyncio.sleep(self._settings.poll_seconds)
            logger.info("provisioner running")
            try:
                while True:
                    # Also proves this connection, and with it the lock, is still
                    # held. If it is not, the error ends the process and a fresh
                    # one starts by waiting for the lock again.
                    await connection.fetchval("SELECT 1")
                    await self.tick()
                    self._heartbeat()
                    await asyncio.sleep(self._settings.poll_seconds)
            finally:
                await self.stop()

    def _heartbeat(self) -> None:
        if self._settings.heartbeat_file:
            Path(self._settings.heartbeat_file).touch()

    async def tick(self) -> None:
        """Starts a task for every row that needs work and does not have one."""
        for row_id, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[row_id]
        for store in await repo.list_stores(self._pool, ("provisioning", "deleting")):
            if store.id not in self._tasks:
                self._tasks[store.id] = asyncio.create_task(self._work_on_store(store.id), name=f"store-{store.slug}")
        for server in await repo.list_hosted_work(self._pool):
            if server.id not in self._tasks:
                self._tasks[server.id] = asyncio.create_task(self._work_on_server(server.id), name=f"mcp-{server.slug}")

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def idle(self) -> None:
        """Waits for the tasks started so far. For tests."""
        await asyncio.gather(*self._tasks.values())

    async def _work_on_store(self, store_id: UUID) -> None:
        async with self._slots:
            # It may have waited for a free slot; see what is wanted now.
            store = await repo.get_store(self._pool, store_id)
            if store is None or store.status not in ("provisioning", "deleting"):
                return
            creating = store.status == "provisioning"
            await self._attempt(
                f"store {store.slug}",
                self._provision(store) if creating else self._teardown(store),
                creating=creating,
                fail=lambda reason: repo.finish_store(self._pool, store.id, "failed", reason),
                say=lambda detail: repo.set_store_detail(self._pool, store.id, detail),
            )

    async def _work_on_server(self, server_id: UUID) -> None:
        async with self._slots:
            server = await repo.get_server(self._pool, server_id)
            if server is None or server.hosted_status not in ("deploying", "deleting"):
                return
            creating = server.hosted_status == "deploying"
            await self._attempt(
                f"hosted server {server.slug}",
                self._deploy(server) if creating else self._remove(server),
                creating=creating,
                fail=lambda reason: repo.finish_hosted(self._pool, server.id, "failed", reason),
                say=lambda detail: repo.set_hosted_detail(self._pool, server.id, detail),
            )

    async def _attempt(self, what: str, work: Awaitable[None], *, creating: bool, fail: Callable[[str], Awaitable[Any]], say: Say) -> None:
        """Runs one pass of work and sorts its failures into "give up" and "try again"."""
        try:
            await work
            return
        except asyncio.CancelledError:
            raise
        except Failed as failure:
            reason = str(failure)
        except HelmError as error:
            reason = f"Helm could not install it: {error}"
        except KubeError as error:
            if not (creating and 400 <= error.status < 500 and error.status not in (409, 429)):
                reason = None
                error_text = str(error)
            else:
                # Not allowed, or not valid: asking again will not change the answer.
                reason = f"Kubernetes refused: {error.message}"
        except Exception as error:
            reason, error_text = None, str(error)
        if reason is not None and creating:
            logger.warning("%s failed: %s", what, reason)
            await fail(reason)
        else:
            # The Kubernetes API or the database hiccuped. The row stays as it is
            # and the next tick starts again from the top; the timeout still applies.
            text = reason or error_text
            logger.warning("%s: interrupted (%s), will try again", what, text)
            await say(f"Trying again after an error: {text}"[:300])

    # --- steps shared by stores and hosted servers ------------------------------------

    def _deadline(self, started_at: datetime) -> Callable[[str], None]:
        """Returns a check that gives up once the timeout, counted from when the attempt began, has passed."""
        deadline = started_at + timedelta(seconds=self._settings.timeout_seconds)
        minutes = f"{self._settings.timeout_seconds / 60:g} minutes"

        def check(doing: str) -> None:
            if datetime.now(UTC) > deadline:
                raise Failed(f"Gave up after {minutes}. Last seen: {doing}")

        return check

    async def _ensure_namespace(self, namespace: str, label: str, slug: str) -> bool:
        """Creates the namespace and gives this process the installer role in it.

        False if a namespace of that name is still being removed: come back later.
        """
        existing = await self._kube.get_namespace(namespace)
        if existing is None:
            await self._kube.create_namespace(
                namespace,
                {
                    label: slug,
                    # Kubernetes itself refuses any pod here that asks for root,
                    # host access or extra privileges.
                    "pod-security.kubernetes.io/enforce": "restricted",
                },
            )
        elif existing["metadata"].get("labels", {}).get(label) != slug:
            raise Failed(f"A namespace called {namespace} already exists and was not created for this.")
        elif existing.get("status", {}).get("phase") == "Terminating":
            return False
        settings = self._settings
        await self._kube.bind_cluster_role(
            namespace, "store-installer", settings.installer_role, settings.service_account,
            settings.service_account_namespace,
        )
        return True

    async def _clear_interrupted_install(self, namespace: str, release: str) -> str | None:
        """The release's state, after clearing one that was cut off halfway.

        An install interrupted by this process being restarted is left "pending",
        and helm will not touch a release in that state.
        """
        state = await self._helm.status(namespace, release)
        if state and state.startswith("pending"):
            await self._helm.uninstall(namespace, release)
            return None
        return state

    async def _wait_until_ready(self, namespace: str, still_wanted: Callable[[], Awaitable[bool]], check_deadline: Callable[[str], None], say: Say) -> bool:
        """Waits for every Deployment and StatefulSet in the namespace. False if the thing was deleted meanwhile."""
        while True:
            if not await still_wanted():
                return False
            workloads = await self._kube.workloads(namespace)
            if workloads:
                waiting = waiting_on(workloads, await self._kube.pods(namespace), await self._kube.warnings(namespace))
            else:
                waiting = ["its pods"]
            if not waiting:
                return True
            doing = "Waiting for " + ", ".join(waiting)
            check_deadline(doing)
            await say(doing)
            await asyncio.sleep(self._settings.poll_seconds)

    async def _remove_namespace(self, namespace: str, label: str, slug: str, say: Say) -> None:
        """Deletes the namespace if it is ours, and waits until it is gone."""
        while True:
            existing = await self._kube.get_namespace(namespace)
            if existing is None:
                return
            if existing["metadata"].get("labels", {}).get(label) != slug:
                return  # never ours: creating it failed because it already existed
            if existing.get("status", {}).get("phase") != "Terminating":
                # Deleting the namespace takes everything with it: the pods, the
                # volumes, the Secrets, the Ingress and the permission this
                # process was given there.
                await self._kube.delete_namespace(namespace)
            await say("Removing the namespace and everything in it")
            await asyncio.sleep(self._settings.poll_seconds)

    # --- stores ------------------------------------------------------------------------

    async def _provision(self, store: Store) -> None:
        settings = self._settings
        namespace = store.namespace
        say: Say = lambda detail: repo.set_store_detail(self._pool, store.id, detail)  # noqa: E731
        check_deadline = self._deadline(store.started_at)
        check_deadline(store.detail or "nothing had started")

        await say("Creating the namespace")
        if not await self._ensure_namespace(namespace, MANAGED_LABEL, store.slug):
            # A store of the same name was deleted a moment ago. Wait for it to go.
            await say("Waiting for the previous store of this name to finish being removed")
            return

        if await self._clear_interrupted_install(namespace, RELEASE) != "deployed":
            await say("Installing WordPress, WooCommerce and the database")
            host = settings.host_template.format(slug=store.slug)
            await self._helm.install(
                namespace, RELEASE, settings.chart, list(settings.values_files),
                {
                    "store.host": host,
                    "store.title": store.name,
                    "store.adminEmail": store.owner_email,
                    # The store lets only the gateway call its MCP server, and has
                    # to be told which namespace the gateway is in.
                    "networkPolicy.gateway.namespace": settings.service_account_namespace,
                },
            )

        async def still_wanted() -> bool:
            current = await repo.get_store(self._pool, store.id)
            return current is not None and current.status == "provisioning"

        if not await self._wait_until_ready(namespace, still_wanted, check_deadline, say):
            return  # deleted while it was being created

        if settings.url_scheme == "https":
            # The pods being up is not yet the store being reachable: its
            # certificate is issued separately, and until it is there a browser
            # is shown a certificate it does not trust.
            while not (await self._kube.read_secret(namespace, f"{RELEASE}-tls") or {}).get("tls.crt"):
                if not await still_wanted():
                    return
                check_deadline("Waiting for the store's TLS certificate")
                await say("Waiting for the store's TLS certificate")
                await asyncio.sleep(settings.poll_seconds)

        await say("Registering the store's tools in the gateway")
        secrets = await self._kube.read_secret(namespace, f"{RELEASE}-secrets") or {}
        token = secrets.get("mcp-auth-token")
        if not token:
            raise Failed("The store was installed without a token for its MCP server.")
        url = f"http://{RELEASE}-woo-mcp.{namespace}.svc.{settings.cluster_domain}/mcp"
        if not await repo.register_store_server(self._pool, store, url=url, auth_secret=self._cipher.encrypt(token)):
            raise Failed(f"The gateway already has a server with the identifier {store.server_slug}.")

        public = f"{settings.url_scheme}://{settings.host_template.format(slug=store.slug)}"
        if await repo.finish_store(self._pool, store.id, "ready", None, url=public):
            logger.info("store %s is ready at %s", store.slug, public)

    async def _teardown(self, store: Store) -> None:
        say: Say = lambda detail: repo.set_store_detail(self._pool, store.id, detail)  # noqa: E731
        await self._remove_namespace(store.namespace, MANAGED_LABEL, store.slug, say)
        await repo.delete_store(self._pool, store.id)
        logger.info("store %s is gone", store.slug)

    # --- hosted servers ----------------------------------------------------------------

    async def _deploy(self, server: Server) -> None:
        """Runs an admin's image as an MCP server. The gateway already has its row, URL and credential."""
        spec = hosted.HostedSpec.from_json(server.hosted)
        namespace = hosted.namespace(server.slug)
        say: Say = lambda detail: repo.set_hosted_detail(self._pool, server.id, detail)  # noqa: E731
        check_deadline = self._deadline(server.hosted_started_at)
        check_deadline(server.hosted_detail or "nothing had started")

        await say("Creating the namespace")
        if not await self._ensure_namespace(namespace, hosted.LABEL, server.slug):
            await say("Waiting for the previous server of this name to finish being removed")
            return

        # Unlike a store, the install runs on every pass: that is what applies a
        # changed image or setting. Unchanged, it changes nothing.
        await self._clear_interrupted_install(namespace, hosted.RELEASE)
        await say(f"Starting {spec.image}")
        # The gateway made this credential up and will present it on every
        # call. Here it travels the other way, into the server's own Secret.
        token = self._cipher.decrypt(server.auth_secret)
        await self._helm.install(
            namespace, hosted.RELEASE, self._settings.server_chart, [], {},
            secret_values=hosted.chart_values(spec, token, self._settings.service_account_namespace),
        )

        async def still_wanted() -> bool:
            current = await repo.get_server(self._pool, server.id)
            # A new attempt (an edit, a retry) is someone else's to finish.
            return (
                current is not None
                and current.hosted_status == "deploying"
                and current.hosted_started_at == server.hosted_started_at
            )

        if not await self._wait_until_ready(namespace, still_wanted, check_deadline, say):
            return
        if await repo.finish_hosted(self._pool, server.id, "running", None):
            logger.info("hosted server %s is running %s", server.slug, spec.image)

    async def _remove(self, server: Server) -> None:
        say: Say = lambda detail: repo.set_hosted_detail(self._pool, server.id, detail)  # noqa: E731
        await self._remove_namespace(hosted.namespace(server.slug), hosted.LABEL, server.slug, say)
        await repo.delete_hosted(self._pool, server.id)
        logger.info("hosted server %s is gone", server.slug)
