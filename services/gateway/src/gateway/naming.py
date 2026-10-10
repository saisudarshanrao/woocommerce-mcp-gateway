"""How tools from many servers share one namespace.

Two upstream servers may both have a tool called `list_orders`. The gateway
exposes every tool as `<server slug>__<tool name>`, for example
`woo__list_orders`:

  * always, not only when two names collide, so registering a server never
    renames a tool that clients, permissions or audit history already refer to;
  * a slug cannot contain an underscore, so the first "__" in an exposed name is
    always the separator, even if the tool's own name contains "__";
  * the result uses only letters, digits, "_" and "-" and is at most 64
    characters long, which every major model API accepts as a function name. A
    tool that cannot be made to fit is left out and reported, never renamed.
"""

from __future__ import annotations

import re

SEPARATOR = "__"
MAX_LENGTH = 64
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_PORTABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_PATTERN.match(slug))


def exposed_name(slug: str, tool: str) -> str:
    return f"{slug}{SEPARATOR}{tool}"


def split(name: str) -> tuple[str, str] | None:
    """Splits an exposed name into (slug, tool). None if it is not one of ours."""
    slug, separator, tool = name.partition(SEPARATOR)
    if not separator or not tool or not is_valid_slug(slug):
        return None
    return slug, tool


def why_not_exposable(slug: str, tool: str) -> str | None:
    """The reason a tool cannot be exposed under this scheme, or None if it can."""
    if not _PORTABLE_NAME.match(tool):
        return "its name has characters other than letters, digits, _ and -"
    if len(exposed_name(slug, tool)) > MAX_LENGTH:
        return f"its name with the '{slug}{SEPARATOR}' prefix is longer than {MAX_LENGTH} characters"
    return None
