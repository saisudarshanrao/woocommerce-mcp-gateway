"""MCP servers the platform runs itself, from a container image.

An admin gives an image instead of a URL. What follows is nearly the path a
store takes (see provisioner.py): the API records what is wanted, and the
provisioner installs the mcp-server chart into a namespace of its own,
mcp-<slug>. That chart is the one that runs the two demo servers, so a hosted
server gets the same treatment: a ClusterIP Service and no Ingress, resource
limits, a NetworkPolicy that admits only the gateway and by default lets the
container connect to nothing, no Kubernetes credentials, and a non-root user
on a read-only filesystem. Its namespace is held to the "restricted" pod
security standard, so an image cannot ask for more.

The credential runs the other way round from a server added by URL. There, the
admin types in a credential the server already has. Here the gateway makes one
up, stores it encrypted like any other, and hands it to the server: the
provisioner puts it in a Secret in the server's namespace, and the container
receives it as AUTH_TOKEN. An image that checks it (the demo image does) is
protected twice; one that ignores it is still behind the NetworkPolicy.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

RELEASE = "server"
# The label that marks a namespace as created for a hosted server.
LABEL = "mcp-gateway/hosted"

_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{1,254}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Set by the platform; an admin's value for these would be silently overridden or break the server.
RESERVED_ENV = {"PORT", "AUTH_TOKEN", "AUTH_HEADER", "ALLOWED_HOSTS"}


class InvalidSpec(ValueError):
    """The deployment an admin asked for cannot be accepted. The message says why."""


@dataclass(frozen=True)
class HostedSpec:
    """What to run."""

    image: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    port: int = 8080
    # Where its MCP endpoint is.
    path: str = "/mcp"
    # A path Kubernetes can GET to see whether it is healthy. Empty: check that the port is open.
    health_path: str = ""
    # May it open connections to the internet? Off unless the server needs to call an outside API.
    internet: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> HostedSpec:
        data = json.loads(text)
        return cls(**{**data, "args": tuple(data.get("args", ()))})

    def validate(self, allowed_prefixes: tuple[str, ...] = ()) -> None:
        if not _IMAGE.match(self.image):
            raise InvalidSpec("That does not look like a container image, for example ghcr.io/acme/mcp-server:1.2.")
        if allowed_prefixes and not self.image.startswith(allowed_prefixes):
            raise InvalidSpec(f"Images must come from: {', '.join(allowed_prefixes)}")
        if not 1 <= self.port <= 65535:
            raise InvalidSpec("The port must be between 1 and 65535.")
        for which, value in (("MCP path", self.path), ("health path", self.health_path)):
            if value and not re.match(r"^/[A-Za-z0-9._~/-]*$", value):
                raise InvalidSpec(f"The {which} must start with / and be a plain path.")
        if not self.path:
            raise InvalidSpec("The MCP path cannot be empty.")
        if len(self.args) > 20 or any(len(arg) > 500 for arg in self.args):
            raise InvalidSpec("At most 20 arguments of 500 characters each.")
        if len(self.env) > 30:
            raise InvalidSpec("At most 30 environment variables.")
        for name, value in self.env.items():
            if not _ENV_NAME.match(name):
                raise InvalidSpec(f"'{name}' is not a valid environment variable name.")
            if name in RESERVED_ENV:
                raise InvalidSpec(f"{name} is set by the platform and cannot be given here.")
            if len(value) > 2000:
                raise InvalidSpec(f"The value of {name} is too long.")


def namespace(slug: str) -> str:
    return f"mcp-{slug}"


def url(slug: str, spec: HostedSpec, cluster_domain: str) -> str:
    """Where the gateway will reach the server once it is up: its Service, inside the cluster."""
    return f"http://{RELEASE}-mcp.{namespace(slug)}.svc.{cluster_domain}{spec.path}"


def chart_values(spec: HostedSpec, token: str, gateway_namespace: str) -> dict[str, Any]:
    """The spec, as values for the mcp-server chart. Contains the credential: pass it on standard input."""
    return {
        "image": {"reference": spec.image},
        "args": list(spec.args),
        "env": spec.env,
        "port": spec.port,
        "mcpPath": spec.path,
        "healthPath": spec.health_path,
        "auth": {"enabled": True, "header": "Authorization", "token": token},
        "networkPolicy": {
            "gateway": {"namespace": gateway_namespace},
            "egress": {"internet": spec.internet},
        },
    }
