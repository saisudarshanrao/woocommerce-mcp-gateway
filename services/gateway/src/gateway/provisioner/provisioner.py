"""Makes the cluster match the stores table.

The API only ever writes a row: "this store should exist", or "this store
should go". This loop reads those rows and does the work, one task per store,
several at a time:

    provisioning  ->  namespace, permissions, helm install, wait for the pods,
                      register the store's MCP server in the gateway  ->  ready
    deleting      ->  delete the namespace, wait until it is gone, drop the row

Every step first looks at what already exists and only does what is missing.
That one property is what makes the rest safe: pressing "create" twice, retrying
a failed store, and this process being restarted halfway through all come down
to running the same steps again from the top.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import asyncpg

from gateway import repo
from gateway.provisioner.helm import HelmError
from gateway.provisioner.kube import KubeError
from gateway.repo import Store
from gateway.security import CredentialCipher

logger = logging.getLogger(__name__)

# Only one provisioner works at a time, whichever holds this Postgres lock.
LEADER_LOCK_ID = 7_265_001
# The label that marks a namespace as created by the provisioner. The cluster's
# admission policy lets the provisioner touch only namespaces that carry it.
MANAGED_LABEL = "mcp-gateway/store"
RELEASE = "store"


class KubeApi(Protocol):
    async def get_namespace(self, name: str) -> dict | None: ...
    async def create_namespace(self, name: str, labels: dict[str, str]) -> None: ...
    async def delete_namespace(self, name: str) -> None: ...
    async def bind_cluster_role(self, namespace: str, name: str, cluster_role: str, account: str, account_namespace: str) -> None: ...
    async def read_secret(self, namespace: str, name: str) -> dict[str, str] | None: ...
    async def workloads(self, namespace: str) -> list[dict]: ...
    async def pods(self, namespace: str) -> list[dict]: ...


class HelmCli(Protocol):
    async def status(self, namespace: str, release: str) -> str | None: ...
    async def install(self, namespace: str, release: str, chart: str, values_files: list[str], values: dict[str, str]) -> None: ...
    async def uninstall(self, namespace: str, release: str) -> None: ...


@dataclass(frozen=True)
class ProvisionerSettings:
    chart: str = "/charts/store"
    # Values files applied to every store, in order: the environment's file
    # from the chart itself, then the deployment's overrides.
    values_files: tuple[str, ...] = ()
    # The public hostname of a store, with {slug} standing for its identifier.
    host_template: str = "{slug}.localhost"
    url_scheme: str = "http"
    cluster_domain: str = "cluster.local"
    # The ClusterRole holding what installing a store needs, and who to grant it to.
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


def waiting_on(workloads: list[dict], pods: list[dict]) -> list[str]:
    """What is not ready yet, in words. Empty when every workload has all its pods ready."""
    waiting = []
    for workload in workloads:
        wanted = workload["spec"].get("replicas", 1)
        status = workload.get("status", {})
        current = status.get("observedGeneration") == workload["metadata"].get("generation")
        if current and status.get("readyReplicas", 0) >= wanted:
            continue
        name = workload["metadata"]["name"]
        reason = next(
            (c.get("message", "") for c in status.get("conditions", []) if c.get("type") == "ReplicaFailure"), ""
        )
        waiting.append(f"{name} ({reason or pod_state(name, pods) or 'starting'})")
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
                    return state["waiting"]["reason"]
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
        """Starts a task for every store that needs work and does not have one."""
        for store_id, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[store_id]
        for store in await repo.list_stores(self._pool, ("provisioning", "deleting")):
            if store.id not in self._tasks:
                self._tasks[store.id] = asyncio.create_task(self._work(store), name=f"store-{store.slug}")

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def idle(self) -> None:
        """Waits for the tasks started so far. For tests."""
        await asyncio.gather(*self._tasks.values())

    async def _work(self, store: Store) -> None:
        async with self._slots:
            # It may have waited for a free slot; see what is wanted now.
            store = await repo.get_store(self._pool, store.id)
            if store is None or store.status not in ("provisioning", "deleting"):
                return
            try:
                if store.status == "deleting":
                    await self._teardown(store)
                else:
                    await self._provision(store)
            except asyncio.CancelledError:
                raise
            except Failed as failure:
                await self._fail(store, str(failure))
            except HelmError as error:
                await self._fail(store, f"Helm could not install the store: {error}")
            except KubeError as error:
                if store.status == "provisioning" and 400 <= error.status < 500 and error.status not in (409, 429):
                    # Not allowed, or not valid: asking again will not change the answer.
                    await self._fail(store, f"Kubernetes refused: {error.message}")
                else:
                    await self._retry_later(store, error)
            except Exception as error:
                await self._retry_later(store, error)

    async def _fail(self, store: Store, reason: str) -> None:
        logger.warning("store %s failed: %s", store.slug, reason)
        await repo.finish_store(self._pool, store.id, "failed", reason)

    async def _retry_later(self, store: Store, error: Exception) -> None:
        # The Kubernetes API or the database hiccuped. The store stays as it is
        # and the next tick starts again from the top; the timeout still applies.
        logger.warning("store %s: interrupted (%s), will try again", store.slug, error)
        await repo.set_store_detail(self._pool, store.id, f"Trying again after an error: {error}"[:300])

    # --- creating --------------------------------------------------------------

    async def _provision(self, store: Store) -> None:
        settings = self._settings
        namespace = store.namespace
        deadline = store.started_at + timedelta(seconds=settings.timeout_seconds)
        minutes = f"{settings.timeout_seconds / 60:g} minutes"

        def check_deadline(doing: str) -> None:
            if datetime.now(UTC) > deadline:
                raise Failed(f"Gave up after {minutes}. Last seen: {doing}")

        check_deadline(store.detail or "nothing had started")

        await self._say(store, "Creating the namespace")
        existing = await self._kube.get_namespace(namespace)
        if existing is None:
            await self._kube.create_namespace(
                namespace,
                {
                    MANAGED_LABEL: store.slug,
                    # Kubernetes itself refuses any pod here that asks for root,
                    # host access or extra privileges.
                    "pod-security.kubernetes.io/enforce": "restricted",
                },
            )
        elif existing["metadata"].get("labels", {}).get(MANAGED_LABEL) != store.slug:
            raise Failed(f"A namespace called {namespace} already exists and was not created for this store.")
        elif existing.get("status", {}).get("phase") == "Terminating":
            # A store of the same name was deleted a moment ago. Wait for it to go.
            await self._say(store, "Waiting for the previous store of this name to finish being removed")
            return
        await self._kube.bind_cluster_role(
            namespace, "store-installer", settings.installer_role, settings.service_account,
            settings.service_account_namespace,
        )

        release = await self._helm.status(namespace, RELEASE)
        if release and release.startswith("pending"):
            # An install was cut off halfway, most likely by this process being
            # restarted. Helm will not touch a release in that state, so clear it.
            await self._helm.uninstall(namespace, RELEASE)
            release = None
        if release != "deployed":
            await self._say(store, "Installing WordPress, WooCommerce and the database")
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

        while True:
            current = await repo.get_store(self._pool, store.id)
            if current is None or current.status != "provisioning":
                return  # deleted while it was being created
            workloads = await self._kube.workloads(namespace)
            waiting = waiting_on(workloads, await self._kube.pods(namespace)) if workloads else ["the store's pods"]
            if not waiting:
                break
            doing = "Waiting for " + ", ".join(waiting)
            check_deadline(doing)
            await self._say(store, doing)
            await asyncio.sleep(settings.poll_seconds)

        await self._say(store, "Registering the store's tools in the gateway")
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

    # --- removing ----------------------------------------------------------------

    async def _teardown(self, store: Store) -> None:
        namespace = store.namespace
        while True:
            existing = await self._kube.get_namespace(namespace)
            if existing is None:
                break
            if existing["metadata"].get("labels", {}).get(MANAGED_LABEL) != store.slug:
                break  # never ours: provisioning failed because it already existed
            if existing.get("status", {}).get("phase") != "Terminating":
                # Deleting the namespace takes everything with it: the pods, the
                # database and its volumes, the Secrets, the Ingress and the
                # permission this process was given there.
                await self._kube.delete_namespace(namespace)
            await self._say(store, "Removing the namespace and everything in it")
            await asyncio.sleep(self._settings.poll_seconds)
        await repo.delete_store(self._pool, store.id)
        logger.info("store %s is gone", store.slug)

    async def _say(self, store: Store, detail: str) -> None:
        await repo.set_store_detail(self._pool, store.id, detail)
