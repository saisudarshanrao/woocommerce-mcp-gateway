"""Entry point of the provisioner: `python -m gateway.provisioner`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from gateway import db
from gateway.provisioner.helm import Helm
from gateway.provisioner.kube import Kube
from gateway.provisioner.provisioner import Provisioner, ProvisionerSettings
from gateway.security import CredentialCipher


def settings_from_env() -> ProvisionerSettings:
    env = os.environ
    return ProvisionerSettings(
        chart=env.get("STORE_CHART", "/charts/store"),
        values_files=tuple(f.strip() for f in env.get("STORE_VALUES_FILES", "").split(",") if f.strip()),
        server_chart=env.get("SERVER_CHART", "/charts/mcp-server"),
        host_template=env.get("STORE_HOST_TEMPLATE", "{slug}.localhost"),
        url_scheme=env.get("STORE_URL_SCHEME", "http"),
        cluster_domain=env.get("CLUSTER_DOMAIN", "cluster.local"),
        installer_role=env.get("STORE_INSTALLER_ROLE", "gateway-store-installer"),
        service_account=env.get("SERVICE_ACCOUNT", "gateway-provisioner"),
        service_account_namespace=env.get("POD_NAMESPACE", "gateway"),
        timeout_seconds=float(env.get("PROVISION_TIMEOUT_SECONDS", "600")),
        max_concurrent=int(env.get("MAX_CONCURRENT_STORES", "3")),
        poll_seconds=float(env.get("POLL_SECONDS", "2")),
        heartbeat_file=env.get("HEARTBEAT_FILE", ""),
    )


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # The HTTP client logs every request, which here is several a second.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    pool = db.Database(os.environ["DATABASE_URL"])
    await pool.connect(retry_for_seconds=90)
    # The API runs the same migrations. A lock makes whichever starts first do it.
    await db.migrate(pool)
    kube = Kube.in_cluster()
    provisioner = Provisioner(
        pool, CredentialCipher(os.environ["ENCRYPTION_KEY"]), kube, Helm(), settings_from_env()
    )

    work = asyncio.create_task(provisioner.run())
    # Kubernetes sends SIGTERM to stop a pod. Stores in progress are simply
    # picked up again by the next provisioner.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, work.cancel)
    try:
        await work
    except asyncio.CancelledError:
        pass
    finally:
        await kube.close()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyError as missing:
        sys.exit(f"environment variable {missing} is required")
