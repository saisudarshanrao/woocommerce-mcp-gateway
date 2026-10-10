"""Runs the helm command, the same one `make store-up` runs for the first store.

Helm finds the cluster the same way the Kube class does: from the pod's
environment and its ServiceAccount token.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping


class HelmError(Exception):
    """helm exited with an error. The message is the end of what it printed."""


class Helm:
    def __init__(self, binary: str = "helm", timeout_seconds: float = 120):
        self._binary = binary
        self._timeout = timeout_seconds

    async def _run(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            self._binary, *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            async with asyncio.timeout(self._timeout):
                out, err = await process.communicate()
        except TimeoutError:
            process.kill()
            raise HelmError(f"helm {arguments[0]} did not finish within {self._timeout:g} seconds") from None
        if process.returncode != 0:
            raise HelmError(err.decode(errors="replace").strip()[-400:] or f"helm {arguments[0]} failed")
        return out.decode()

    async def status(self, namespace: str, release: str) -> str | None:
        """The release's state ("deployed", "failed", "pending-install", ...), or None if there is none."""
        try:
            output = await self._run("status", release, "--namespace", namespace, "--output", "json")
        except HelmError as error:
            if "not found" in str(error):
                return None
            raise
        return json.loads(output)["info"]["status"]

    async def install(
        self, namespace: str, release: str, chart: str, values_files: list[str], values: Mapping[str, str]
    ) -> None:
        """Installs the chart, or brings an existing release up to date. Returns without waiting for pods."""
        arguments = ["upgrade", "--install", release, chart, "--namespace", namespace]
        for file in values_files:
            arguments += ["--values", file]
        for key, value in values.items():
            # --set-string: a store called "123" or "true" must stay a string.
            arguments += ["--set-string", f"{key}={value.replace(',', r'\,')}"]
        await self._run(*arguments)

    async def uninstall(self, namespace: str, release: str) -> None:
        await self._run("uninstall", release, "--namespace", namespace, "--ignore-not-found")
