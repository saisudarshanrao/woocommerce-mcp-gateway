import base64
import hashlib
import hmac

from woo_mcp import oauth

URL = "http://store.localhost/wp-json/wc/v3/orders"


def test_base_string_sorts_parameters_and_encodes_twice():
    base = oauth.signature_base_string("get", URL, {"search": "a b+c", "per_page": 5})

    # Method upper-cased, URL encoded once, each parameter encoded and then the
    # joined list encoded again: a space ends up as %2520 and "=" as %3D.
    assert base == (
        "GET&http%3A%2F%2Fstore.localhost%2Fwp-json%2Fwc%2Fv3%2Forders&per_page%3D5%26search%3Da%2520b%252Bc"
    )


def test_percent_encoding_leaves_only_unreserved_characters():
    assert oauth.percent_encode("a-b.c_d~e") == "a-b.c_d~e"
    assert oauth.percent_encode("2026-10-09T00:00:00") == "2026-10-09T00%3A00%3A00"
    assert oauth.percent_encode("café/100%") == "caf%C3%A9%2F100%25"


def test_signature_is_hmac_sha256_keyed_with_the_secret_and_an_ampersand():
    signed = oauth.sign("GET", URL, {"per_page": 5}, "ck_key", "cs_secret", nonce="n1", timestamp=1791500000)

    expected_base = oauth.signature_base_string(
        "GET",
        URL,
        {
            "per_page": 5,
            "oauth_consumer_key": "ck_key",
            "oauth_nonce": "n1",
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": "1791500000",
        },
    )
    expected = base64.b64encode(hmac.new(b"cs_secret&", expected_base.encode(), hashlib.sha256).digest()).decode()
    assert signed["oauth_signature"] == expected
    assert signed["oauth_consumer_key"] == "ck_key"
    assert "cs_secret" not in "".join(signed.values())


def test_any_change_to_the_request_changes_the_signature():
    def signature(method="GET", url=URL, query=None, secret="cs_secret"):
        return oauth.sign(method, url, query or {"page": 1}, "ck", secret, nonce="n", timestamp=1)["oauth_signature"]

    original = signature()
    assert signature(method="POST") != original
    assert signature(url=URL + "/41") != original
    assert signature(query={"page": 2}) != original
    assert signature(secret="cs_other") != original


def test_each_request_gets_a_fresh_nonce():
    first = oauth.sign("GET", URL, {}, "ck", "cs")
    second = oauth.sign("GET", URL, {}, "ck", "cs")
    assert first["oauth_nonce"] != second["oauth_nonce"]


def test_authorization_header_quotes_and_encodes_each_value():
    header = oauth.authorization_header({"oauth_signature": "ab+c/d=", "oauth_consumer_key": "ck"})

    assert header == 'OAuth oauth_consumer_key="ck", oauth_signature="ab%2Bc%2Fd%3D"'
