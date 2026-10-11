"""The few Kubernetes API calls the provisioner makes, over plain HTTPS.

Inside a pod, Kubernetes provides everything needed to call its API: the
address in two environment variables, and, mounted as files, a token for the
pod's ServiceAccount and the certificate authority that signed the API's
certificate. What the token is allowed to do is decided by RBAC, in the chart.
"""

from __future__ import annotations

import base64
import os
import ssl
from pathlib import Path
from typing import Any

import httpx2

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubeError(Exception):
    """The Kubernetes API refused a request. `status` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Kube:
    def __init__(self, http: httpx2.AsyncClient, token_file: Path | None = None):
        self._http = http
        self._token_file = token_file

    @classmethod
    def in_cluster(cls) -> Kube:
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        if ":" in host:
            host = f"[{host}]"
        http = httpx2.AsyncClient(
            base_url=f"https://{host}:{port}",
            verify=ssl.create_default_context(cafile=str(SERVICE_ACCOUNT_DIR / "ca.crt")),
            timeout=15,
        )
        return cls(http, SERVICE_ACCOUNT_DIR / "token")

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: dict | None = None, *, missing_ok: bool = False) -> Any:
        headers = {}
        if self._token_file:
            # Read every time: Kubernetes replaces the token about once an hour.
            headers["Authorization"] = f"Bearer {self._token_file.read_text().strip()}"
        response = await self._http.request(method, path, json=body, headers=headers)
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise KubeError(response.status_code, message)
        return response.json()

    # --- namespaces ----------------------------------------------------------

    async def get_namespace(self, name: str) -> dict | None:
        return await self._request("GET", f"/api/v1/namespaces/{name}", missing_ok=True)

    async def create_namespace(self, name: str, labels: dict[str, str]) -> None:
        await self._request("POST", "/api/v1/namespaces", {"metadata": {"name": name, "labels": labels}})

    async def delete_namespace(self, name: str) -> None:
        """Starts deleting a namespace and everything in it. Kubernetes finishes in the background."""
        await self._request("DELETE", f"/api/v1/namespaces/{name}", missing_ok=True)

    # --- inside a namespace ---------------------------------------------------

    async def bind_cluster_role(self, namespace: str, name: str, cluster_role: str, account: str, account_namespace: str) -> None:
        """Gives a ServiceAccount the permissions of `cluster_role`, inside this one namespace."""
        binding = {
            "metadata": {"name": name},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role},
            "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": account_namespace}],
        }
        try:
            await self._request("POST", f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings", binding)
        except KubeError as error:
            if error.status != 409:  # 409: it is already there
                raise

    async def read_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        secret = await self._request("GET", f"/api/v1/namespaces/{namespace}/secrets/{name}", missing_ok=True)
        if secret is None:
            return None
        return {key: base64.b64decode(value).decode() for key, value in (secret.get("data") or {}).items()}

    async def workloads(self, namespace: str) -> list[dict]:
        """Every Deployment and StatefulSet in the namespace."""
        deployments = await self._request("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments")
        statefulsets = await self._request("GET", f"/apis/apps/v1/namespaces/{namespace}/statefulsets")
        return [*deployments["items"], *statefulsets["items"]]

    async def pods(self, namespace: str) -> list[dict]:
        return (await self._request("GET", f"/api/v1/namespaces/{namespace}/pods"))["items"]

    async def warnings(self, namespace: str) -> dict[str, str]:
        """The latest warning Kubernetes recorded about each object in the namespace, by the object's name.

        Some refusals show up nowhere else. A StatefulSet whose volume is
        refused by a quota, for example, simply has no pod, and only its
        events say why.
        """
        events = await self._request("GET", f"/api/v1/namespaces/{namespace}/events?fieldSelector=type%3DWarning")
        latest: dict[str, str] = {}
        for event in sorted(events["items"], key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or ""):
            latest[event["involvedObject"]["name"]] = event.get("message", "")
        return latest
