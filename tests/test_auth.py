from __future__ import annotations

import auth


def _req(path="/api/car/home", method="GET", host="192.168.87.50",
         headers=None):
    """A stand-in for the parts of a Starlette Request that auth.decide reads."""
    return auth.Incoming(path=path, method=method, client_host=host,
                         headers=headers or {})


def test_loopback_reads_need_no_token():
    assert auth.decide(_req(host="127.0.0.1"), tokens=("secret",)) is None


def test_lan_reads_need_a_token():
    assert auth.decide(_req(), tokens=("secret",)) == (401, "unauthorized")


def test_lan_reads_pass_with_the_header():
    assert auth.decide(
        _req(headers={"x-api-key": "secret"}), tokens=("secret",)) is None


def test_lan_reads_pass_with_the_cookie():
    assert auth.decide(
        _req(headers={"cookie": "api_token=secret"}), tokens=("secret",)) is None


def test_any_of_the_three_tokens_is_accepted():
    assert auth.decide(
        _req(headers={"x-api-key": "mcp"}),
        tokens=("browser", "ha", "mcp")) is None


def test_an_unset_token_is_never_a_valid_credential():
    """Three tokens are configured as ('secret', '', ''). An empty presented
    value must not match the empty slots -- that would make an absent header
    a valid credential the moment any one token is left unconfigured."""
    assert auth.decide(
        _req(headers={"x-api-key": ""}), tokens=("secret", "", "")
    ) == (401, "unauthorized")


def test_ha_and_mcp_need_a_token_even_from_loopback():
    """These are control surfaces, not the local UI."""
    for path in ("/api/ha/state", "/mcp"):
        assert auth.decide(
            _req(path=path, host="127.0.0.1"), tokens=("secret",)
        ) == (401, "unauthorized"), path


def test_cross_site_mutations_are_refused_even_from_loopback():
    """The drive-by CSRF form targets 127.0.0.1:8000 from the owner's OWN
    browser, so the loopback exemption alone does not close it."""
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1",
             headers={"sec-fetch-site": "cross-site"}),
        tokens=("secret",)) == (403, "cross-site request refused")


def test_same_origin_mutations_are_allowed():
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1",
             headers={"sec-fetch-site": "same-origin"}),
        tokens=("secret",)) is None


def test_a_missing_sec_fetch_site_is_not_treated_as_cross_site():
    """curl and every non-browser client omit it. Refusing on absence would
    break the CLI and every automation without closing anything -- a browser
    that can forge the header can forge anything."""
    assert auth.decide(
        _req(path="/api/car/wake", method="POST", host="127.0.0.1"),
        tokens=("secret",)) is None


def test_auth_routes_stay_open_from_loopback():
    """Tesla redirects the browser to /auth/callback with the code; a 401
    there strands the whole OAuth flow."""
    assert auth.decide(
        _req(path="/auth/callback", host="127.0.0.1"), tokens=("secret",)) is None
