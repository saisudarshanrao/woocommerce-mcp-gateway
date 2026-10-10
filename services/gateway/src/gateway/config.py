"""Settings, read once from the environment at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(Exception):
    """The environment is missing something the gateway cannot start without."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Secret material that the key for encrypting upstream credentials is derived from.
    encryption_key: str
    # The address people and MCP clients use, for example http://gateway.localhost.
    public_url: str
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    # JSON file listing upstream servers to register on first start.
    seed_servers_file: str = ""
    # Extra Host header values to accept besides the public one (Service names).
    extra_allowed_hosts: tuple[str, ...] = ()
    # Host name patterns of MCP servers inside the cluster, the only private
    # addresses the gateway will connect to. See urlguard.py.
    upstream_internal_hosts: tuple[str, ...] = ()
    upstream_allow_public: bool = True
    # Whether stores can be created from the dashboard. Needs the provisioner running.
    stores_enabled: bool = False
    max_stores_per_user: int = 3
    # What a new store's address will be, with {slug} for its identifier. Shown in the form.
    store_url_template: str = ""
    # Tool calls one API key (or one person in the chat) may make per minute. 0 is no limit.
    rate_limit_per_minute: int = 0
    probe_interval_seconds: float = 10.0
    probe_timeout_seconds: float = 5.0
    session_ttl_hours: int = 12
    invite_ttl_hours: int = 72
    # With more tools than this, the chat gives the model find_tools and
    # call_tool instead of every tool's definition. See toolsearch.py.
    chat_tool_search_above: int = 40
    # The chat agent's model, reached through an OpenAI-compatible API.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def listed(name: str) -> tuple[str, ...]:
            return tuple(item.strip() for item in env.get(name, "").split(",") if item.strip())

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"environment variable {name} is required")
            return value

        return cls(
            database_url=required("DATABASE_URL"),
            encryption_key=required("ENCRYPTION_KEY"),
            public_url=required("PUBLIC_URL").rstrip("/"),
            bootstrap_admin_email=env.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower(),
            bootstrap_admin_password=env.get("BOOTSTRAP_ADMIN_PASSWORD", ""),
            seed_servers_file=env.get("SEED_SERVERS_FILE", "").strip(),
            extra_allowed_hosts=listed("EXTRA_ALLOWED_HOSTS"),
            upstream_internal_hosts=listed("UPSTREAM_INTERNAL_HOSTS"),
            upstream_allow_public=env.get("UPSTREAM_ALLOW_PUBLIC", "true").strip().lower() != "false",
            stores_enabled=env.get("STORES_ENABLED", "false").strip().lower() == "true",
            max_stores_per_user=int(env.get("MAX_STORES_PER_USER", "3")),
            store_url_template=env.get("STORE_URL_TEMPLATE", "").strip(),
            rate_limit_per_minute=int(env.get("RATE_LIMIT_PER_MINUTE", "0")),
            probe_interval_seconds=float(env.get("PROBE_INTERVAL_SECONDS", "10")),
            probe_timeout_seconds=float(env.get("PROBE_TIMEOUT_SECONDS", "5")),
            session_ttl_hours=int(env.get("SESSION_TTL_HOURS", "12")),
            invite_ttl_hours=int(env.get("INVITE_TTL_HOURS", "72")),
            chat_tool_search_above=int(env.get("CHAT_TOOL_SEARCH_ABOVE", "40")),
            llm_base_url=env.get("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_model=env.get("LLM_MODEL", "").strip(),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8080")),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )
