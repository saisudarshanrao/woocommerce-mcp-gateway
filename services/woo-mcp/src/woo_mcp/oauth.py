"""Request signing for the WooCommerce REST API: one-legged OAuth 1.0a.

WooCommerce accepts its API key pair as HTTP Basic auth only over HTTPS. Inside
the cluster the store is reached over plain HTTP, because TLS ends at the
ingress, so each request is signed instead. The consumer secret is the HMAC key
and never leaves this process; what travels is the consumer key, a timestamp, a
one-time nonce and the signature over all of it.

The signature covers three things, joined by "&":

    METHOD & percent-encoded URL & percent-encoded sorted parameters

This follows RFC 5849 section 3.4 and matches WooCommerce's own check in
WC_REST_Authentication::check_oauth_signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from urllib.parse import quote

SIGNATURE_METHOD = "HMAC-SHA256"


def percent_encode(value: object) -> str:
    """RFC 3986 encoding: everything except letters, digits and - . _ ~"""
    return quote(str(value), safe="-._~")


def signature_base_string(method: str, url: str, params: Mapping[str, object]) -> str:
    """The exact string that both sides run through HMAC."""
    pairs = sorted((percent_encode(key), percent_encode(value)) for key, value in params.items())
    normalized = "&".join(f"{key}={value}" for key, value in pairs)
    return "&".join([method.upper(), percent_encode(url), percent_encode(normalized)])


def sign(
    method: str,
    url: str,
    query: Mapping[str, object],
    consumer_key: str,
    consumer_secret: str,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Returns the oauth_* parameters for one request, signature included.

    `url` is the URL WooCommerce will reconstruct on its side, without the query
    string. `query` is the request's query parameters: they are part of what is
    signed, so changing any of them in transit invalidates the signature. A
    JSON request body is not signed.

    `nonce` and `timestamp` exist so tests can produce a fixed signature.
    WooCommerce rejects a timestamp more than 15 minutes off and a nonce it has
    already seen, which is what stops a captured request being replayed.
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": SIGNATURE_METHOD,
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
    }
    base_string = signature_base_string(method, url, {**query, **oauth})
    # The key is "<consumer secret>&<token secret>". One-legged OAuth has no
    # token, so the second half is empty.
    key = f"{consumer_secret}&".encode()
    digest = hmac.new(key, base_string.encode(), hashlib.sha256).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return oauth


def authorization_header(oauth: Mapping[str, str]) -> str:
    """Formats signed parameters as an Authorization header value.

    Sending them in a header rather than the query string keeps the key and
    signature out of the store's access log.
    """
    return "OAuth " + ", ".join(f'{key}="{percent_encode(value)}"' for key, value in sorted(oauth.items()))
