"""Entry point: python -m woo_mcp"""

from __future__ import annotations

import logging
import sys

import uvicorn

from woo_mcp.app import HEALTH_PATH, build_app
from woo_mcp.config import ConfigError, Settings


class _SkipHealthChecks(logging.Filter):
    """Keeps Kubernetes probes, which arrive every few seconds, out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return HEALTH_PATH not in record.getMessage()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"woo-mcp cannot start: {error}")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_SkipHealthChecks())
    # The HTTP client logs every request URL at INFO, which would repeat what
    # this service already logs for each call to the store.
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
