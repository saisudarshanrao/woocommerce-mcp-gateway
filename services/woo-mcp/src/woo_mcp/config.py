"""Settings, read once from the environment at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Host headers accepted when nothing else is configured: the names a developer
# uses to reach a server running on their own machine.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class ConfigError(Exception):
    """The environment is missing something the server cannot start without."""


@dataclass(frozen=True)
class Settings:
    # Where requests to the store are sent. Inside the cluster this is the
    # WordPress Service, reached over plain HTTP.
    store_internal_url: str
    # The URL WordPress believes it has (its "home" URL). WooCommerce verifies
    # request signatures against this URL, not against the one a request
    # happened to arrive on, so the two are configured separately.
    store_public_url: str
    consumer_key: str
    consumer_secret: str
    # The bearer token a caller must present. Only the gateway holds it.
    auth_token: str
    # Host header values to accept, as bare names; see app.py.
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    host: str = "0.0.0.0"
    port: int = 8080
    request_timeout_seconds: float = 15.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"environment variable {name} is required")
            return value

        internal_url = required("STORE_INTERNAL_URL").rstrip("/")
        hosts = [host.strip() for host in env.get("MCP_ALLOWED_HOSTS", "").split(",") if host.strip()]
        return cls(
            store_internal_url=internal_url,
            # When the store is reached at its public URL, as in local
            # development through the ingress, one variable is enough.
            store_public_url=(env.get("STORE_PUBLIC_URL", "").strip() or internal_url).rstrip("/"),
            consumer_key=required("WC_CONSUMER_KEY"),
            consumer_secret=required("WC_CONSUMER_SECRET"),
            auth_token=required("MCP_AUTH_TOKEN"),
            allowed_hosts=tuple(hosts) or LOCAL_HOSTS,
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8080")),
            request_timeout_seconds=float(env.get("STORE_REQUEST_TIMEOUT_SECONDS", "15")),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )
