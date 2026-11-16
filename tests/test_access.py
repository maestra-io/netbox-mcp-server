"""Tests for Cloudflare Access mode (writes/access.py) and write attribution.

Tokens are signed with a local key and served from a mock JWKS endpoint, the
same shape as https://<team>.cloudflareaccess.com/cdn-cgi/access/certs.
"""

import asyncio
import logging
import time

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from joserfc import jwk
from starlette.testclient import TestClient

from netbox_mcp_server import server as upstream
from netbox_mcp_server.writes import access, write_tools

TEAM = "maestra-io.cloudflareaccess.com"
AUD = "aud-tag-netbox"
CONFIG = access.AccessConfig(aud=AUD, team_domain=TEAM)
EMAIL = "ivonin@maestra.io"

KEY = RSAKeyPair.generate()
OTHER_KEY = RSAKeyPair.generate()


def _jwk(pair: RSAKeyPair, kid: str) -> dict:
    return {**jwk.RSAKey.import_key(pair.public_key).as_dict(), "kid": kid, "alg": "RS256"}


class Jwks:
    """Mock JWKS endpoint: counts fetches, can be swapped or broken."""

    def __init__(self, keys: list[dict]) -> None:
        self.keys = keys
        self.fetches = 0
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{TEAM}/cdn-cgi/access/certs"
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": self.keys})


def _verifier(jwks: Jwks) -> access.CloudflareAccessVerifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(jwks.handler))
    return access.CloudflareAccessVerifier(CONFIG, http_client=client)


def _token(pair: RSAKeyPair = KEY, kid: str = "k1", **overrides) -> str:
    claims = {"email": EMAIL, "type": "app", **overrides}
    return pair.create_token(
        subject="user-uuid",
        issuer=overrides.pop("iss", f"https://{TEAM}"),
        audience=overrides.pop("aud", [AUD]),
        additional_claims={k: v for k, v in claims.items() if k not in ("iss", "aud")},
        kid=kid,
    )


def _verify(verifier, token):
    return asyncio.run(verifier.verify_token(token))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_config_off_when_neither_variable_is_defined():
    assert access.read_config({}) is None


def test_config_normalises_team_domain():
    config = access.read_config(
        {access.AUD_ENV: " tag ", access.TEAM_DOMAIN_ENV: " Maestra-IO.cloudflareaccess.com "}
    )
    assert config == access.AccessConfig(aud="tag", team_domain=TEAM)


@pytest.mark.parametrize(
    "env",
    [
        {access.AUD_ENV: "tag"},
        {access.TEAM_DOMAIN_ENV: TEAM},
        {access.AUD_ENV: "", access.TEAM_DOMAIN_ENV: TEAM},
        {access.AUD_ENV: "  "},
        {access.AUD_ENV: "tag", access.TEAM_DOMAIN_ENV: ""},
        {access.AUD_ENV: "tag", access.TEAM_DOMAIN_ENV: f"https://{TEAM}"},
        {access.AUD_ENV: "tag", access.TEAM_DOMAIN_ENV: f"{TEAM}/x"},
        {access.AUD_ENV: "tag", access.TEAM_DOMAIN_ENV: "evil.example.com"},
    ],
)
def test_half_or_bad_config_is_an_error(env):
    with pytest.raises(ValueError, match="CF_ACCESS_"):
        access.read_config(env)


@pytest.fixture
def fresh_mcp(monkeypatch):
    monkeypatch.setattr(access, "_enabled", False)
    return FastMCP(name="t")


def test_enable_off_leaves_auth_alone(fresh_mcp):
    assert access.enable_from_env({}, fresh_mcp, lambda: ("stdio", False)) is None
    assert fresh_mcp.auth is None
    assert not access.enabled()


@pytest.mark.parametrize(
    ("env", "settings"),
    [
        ({access.AUD_ENV: "tag"}, ("http", False)),
        ({access.AUD_ENV: AUD, access.TEAM_DOMAIN_ENV: TEAM}, ("stdio", False)),
        ({access.AUD_ENV: AUD, access.TEAM_DOMAIN_ENV: TEAM}, ("http", True)),
    ],
)
def test_enable_refuses_to_start(fresh_mcp, env, settings):
    with pytest.raises(SystemExit):
        access.enable_from_env(env, fresh_mcp, lambda: settings)
    assert fresh_mcp.auth is None
    assert not access.enabled()


def test_enable_sets_the_verifier(fresh_mcp):
    access.enable_from_env(
        {access.AUD_ENV: AUD, access.TEAM_DOMAIN_ENV: TEAM}, fresh_mcp, lambda: ("http", False)
    )
    assert isinstance(fresh_mcp.auth, access.CloudflareAccessVerifier)
    assert fresh_mcp.auth.jwks_uri == f"https://{TEAM}/cdn-cgi/access/certs"
    assert fresh_mcp.auth.issuer == f"https://{TEAM}"
    assert access.enabled()


# --------------------------------------------------------------------------
# Token verification
# --------------------------------------------------------------------------


def test_valid_token_carries_the_email():
    result = _verify(_verifier(Jwks([_jwk(KEY, "k1")])), _token())
    assert result is not None
    assert result.claims["email"] == EMAIL


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": ["another-app"]},
        {"iss": "https://other.cloudflareaccess.com"},
        {"exp": int(time.time()) - 5},
        {"nbf": int(time.time()) + 120},
        {"nbf": "soon"},
    ],
)
def test_claim_failures_are_rejected(overrides):
    assert _verify(_verifier(Jwks([_jwk(KEY, "k1")])), _token(**overrides)) is None


def test_nbf_within_clock_tolerance_is_accepted():
    token = _token(nbf=int(time.time()) + 10)
    assert _verify(_verifier(Jwks([_jwk(KEY, "k1")])), token) is not None


def test_token_without_exp_is_rejected():
    from joserfc import jwt

    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "email": EMAIL, "sub": "x"}
    key = jwk.RSAKey.import_key(KEY.private_key.get_secret_value())
    token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    assert _verify(_verifier(Jwks([_jwk(KEY, "k1")])), token) is None


def test_wrong_signature_is_rejected():
    # Signed by another key under the published kid.
    assert _verify(_verifier(Jwks([_jwk(KEY, "k1")])), _token(pair=OTHER_KEY)) is None


def test_hs256_token_is_rejected():
    from joserfc import jwt

    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "email": EMAIL, "exp": int(time.time()) + 60}
    token = jwt.encode({"alg": "HS256", "kid": "k1"}, claims, jwk.OctKey.import_key("s" * 32))
    assert _verify(_verifier(Jwks([_jwk(KEY, "k1")])), token) is None


def test_key_rotation_is_picked_up_on_unknown_kid():
    jwks = Jwks([_jwk(KEY, "k1")])
    verifier = _verifier(jwks)
    assert _verify(verifier, _token()) is not None
    assert _verify(verifier, _token()) is not None
    assert jwks.fetches == 1  # cached

    jwks.keys = [_jwk(KEY, "k1"), _jwk(OTHER_KEY, "k2")]
    assert _verify(verifier, _token(pair=OTHER_KEY, kid="k2")) is not None
    assert jwks.fetches == 2


def test_jwks_outage_fails_closed_and_is_logged(caplog):
    jwks = Jwks([_jwk(KEY, "k1")])
    jwks.down = True
    with caplog.at_level(logging.WARNING, logger=access.__name__):
        assert _verify(_verifier(jwks), _token()) is None
    assert "JWKS fetch failed" in caplog.text


# --------------------------------------------------------------------------
# The HTTP gate, end to end through FastMCP
# --------------------------------------------------------------------------

_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_WHOAMI = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "whoami", "arguments": {}},
}


@pytest.fixture
def gated_client():
    mcp = FastMCP(name="t")

    @mcp.tool
    def whoami() -> str:
        return access.verified_email() or "nobody"

    mcp.auth = _verifier(Jwks([_jwk(KEY, "k1")]))
    with TestClient(mcp.http_app(stateless_http=True, json_response=True)) as client:
        yield client


def _call(client, headers):
    return client.post("/mcp/", json=_WHOAMI, headers={**_HEADERS, **headers})


def test_gate_rejects_a_request_without_the_access_header(gated_client):
    assert _call(gated_client, {}).status_code == 401


def test_gate_ignores_a_valid_jwt_sent_as_authorization(gated_client):
    assert _call(gated_client, {"Authorization": f"Bearer {_token()}"}).status_code == 401


def test_gate_rejects_a_forged_access_header(gated_client):
    assert (
        _call(gated_client, {"Cf-Access-Jwt-Assertion": _token(pair=OTHER_KEY)}).status_code == 401
    )


def test_gate_rejects_a_repeated_access_header(gated_client):
    headers = list(_HEADERS.items()) + [("Cf-Access-Jwt-Assertion", _token())] * 2
    response = gated_client.post("/mcp/", json=_WHOAMI, headers=headers)
    assert response.status_code == 401


def test_gate_passes_the_verified_email_to_tools(gated_client):
    # A forged Authorization next to a valid Access header must not win either.
    response = _call(
        gated_client, {"Cf-Access-Jwt-Assertion": _token(), "Authorization": "Bearer junk"}
    )
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == EMAIL


def test_service_token_has_no_person(gated_client):
    token = _token(email=None, common_name="svc.access")
    response = _call(gated_client, {"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == "nobody"


# --------------------------------------------------------------------------
# Write tools in Access mode: refusal and changelog attribution
# --------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.calls = []

    def create(self, endpoint, data):
        self.calls.append(("create", endpoint, data))
        return {"id": 42, **data}

    def update(self, endpoint, id, data):
        self.calls.append(("update", endpoint, id, data))
        return {"id": id, **data}

    def get(self, endpoint, id=None, params=None, fallback_endpoint=None):
        self.calls.append(("get", endpoint, id))
        return {"id": id, "display": "thing"}


@pytest.fixture
def access_mode(monkeypatch):
    """Access mode on; set ``.email`` to the verified person (None = service token)."""
    state = type("State", (), {"email": EMAIL})()
    monkeypatch.setattr(access, "_enabled", True)
    monkeypatch.setattr(access, "verified_email", lambda: state.email)
    client = FakeClient()
    monkeypatch.setattr(upstream, "netbox", client)
    state.client = client
    return state


def test_writes_refuse_without_a_person(access_mode):
    access_mode.email = None
    with pytest.raises(ValueError, match="needs a person"):
        write_tools.netbox_create_object("ipam.prefix", {"prefix": "10.9.0.0/24"})
    with pytest.raises(ValueError, match="needs a person"):
        write_tools.netbox_update_object("ipam.prefix", 1, {"description": "x"})
    with pytest.raises(ValueError, match="needs a person"):
        write_tools.netbox_delete_object("ipam.prefix", 1, "thing")
    assert access_mode.client.calls == []  # nothing reached NetBox, not even the read


def test_create_and_update_carry_the_person_in_the_changelog(access_mode):
    write_tools.netbox_create_object("ipam.prefix", {"prefix": "10.9.0.0/24"})
    write_tools.netbox_update_object("ipam.prefix", 7, {"description": "x"})
    create, update = access_mode.client.calls
    assert create[2] == {"prefix": "10.9.0.0/24", "changelog_message": f"via netbox-mcp by {EMAIL}"}
    assert update[3] == {"description": "x", "changelog_message": f"via netbox-mcp by {EMAIL}"}


def test_caller_message_follows_the_person_and_fits_netbox(access_mode):
    data = {"description": "x", "changelog_message": "per ticket 4137 by someone-else@maestra.io"}
    write_tools.netbox_update_object("ipam.prefix", 7, data)
    message = access_mode.client.calls[0][3]["changelog_message"]
    assert message == f"via netbox-mcp by {EMAIL}: per ticket 4137 by someone-else@maestra.io"
    assert data["changelog_message"].startswith("per ticket")  # caller's dict untouched

    write_tools.netbox_update_object("ipam.prefix", 7, {"changelog_message": "y" * 500})
    message = access_mode.client.calls[1][3]["changelog_message"]
    assert len(message) == write_tools.CHANGELOG_MESSAGE_MAX
    assert message.startswith(f"via netbox-mcp by {EMAIL}: ")


def test_legacy_mode_sends_no_changelog_message(monkeypatch):
    monkeypatch.setattr(access, "_enabled", False)
    client = FakeClient()
    monkeypatch.setattr(upstream, "netbox", client)
    write_tools.netbox_create_object("ipam.prefix", {"prefix": "10.9.0.0/24"})
    assert client.calls[0][2] == {"prefix": "10.9.0.0/24"}


def test_attributed_delete_sends_the_message_in_the_body(access_mode, stub_netbox):
    write_tools.netbox_delete_object("ipam.ipaddress", 1475, "177.28.2.16/32")
    method, path, body, auth = stub_netbox.requests[-1]
    assert (method, path) == ("DELETE", "/api/ipam/ip-addresses/1475/")
    assert body == {"changelog_message": f"via netbox-mcp by {EMAIL}"}
    assert auth == "Token test-token"  # the shared service token stays the credential
