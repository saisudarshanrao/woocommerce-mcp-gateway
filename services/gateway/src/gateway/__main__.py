"""Entry point: python -m gateway"""

from __future__ import annotations

import logging
import sys

import uvicorn

from gateway.app import create_app
from gateway.config import ConfigError, Settings


class _SkipProbes(logging.Filter):
    """Keeps Kubernetes probes, which arrive every few seconds, out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/healthz" not in message and "/readyz" not in message


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"gateway cannot start: {error}")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_SkipProbes())
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    # The MCP client library logs every upstream it opens and closes.
    logging.getLogger("mcp.client").setLevel(logging.WARNING)

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
