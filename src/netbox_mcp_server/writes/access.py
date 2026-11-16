"""Cloudflare Access mode: the HTTP transport behind a Cloudflare tunnel and an
Access application (netbox-mcp.maestrateam.io, issues-maestra#1768).

Off unless ``CF_ACCESS_AUD`` / ``CF_ACCESS_TEAM_DOMAIN`` are set, so the stdio
deployment (Archestra) is unchanged. Same env names, team-domain rule and
half-config refusal as maestra-io/kargo-mcp ``src/access.ts``; repeat a fix
made there.

On: every request to the MCP endpoint needs a valid ``Cf-Access-Jwt-Assertion``
(RS256 against the team's JWKS, ``iss``, ``aud``, ``exp`` required, ``nbf``),
or it gets a 401 before the MCP layer sees it. The token is taken from that
header only: the gate drops any client ``Authorization`` header and hands the
Access JWT to FastMCP's own bearer machinery, so the verified claims reach the
tools through ``get_access_token()``. The NetBox credential stays the shared
service token; the person is written into each change's changelog message
(see write_tools), and write tools refuse a token that names no person.
"""

import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

AUD_ENV = "CF_ACCESS_AUD"
TEAM_DOMAIN_ENV = "CF_ACCESS_TEAM_DOMAIN"
ACCESS_JWT_HEADER = b"cf-access-jwt-assertion"
CLOCK_TOLERANCE_S = 30

# One label, then the fixed suffix. The value becomes both the JWKS URL and the
# expected `iss`, so a typo must not quietly point at another host.
_TEAM_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cloudflareaccess\.com$")


@dataclass(frozen=True)
class AccessConfig:
    aud: str
    team_domain: str


def read_config(env: Any) -> AccessConfig | None:
    """``None`` when the mode is off (neither variable defined).

    Either variable being defined, even empty (an unset Flux var, an optional
    secretKeyRef), means Access was intended: anything short of both is a
    ValueError, so the server refuses to start instead of running open.
    """
    if env.get(AUD_ENV) is None and env.get(TEAM_DOMAIN_ENV) is None:
        return None
    aud = (env.get(AUD_ENV) or "").strip()
    if not aud:
        raise ValueError(f"{TEAM_DOMAIN_ENV} is set but {AUD_ENV} is empty")
    team = (env.get(TEAM_DOMAIN_ENV) or "").strip().lower()
    if not team:
        raise ValueError(f"{AUD_ENV} is set but {TEAM_DOMAIN_ENV} is empty")
    if not _TEAM_DOMAIN.match(team):
        raise ValueError(f"{TEAM_DOMAIN_ENV} must be the bare host <team>.cloudflareaccess.com")
    return AccessConfig(aud=aud, team_domain=team)


class CloudflareAccessVerifier(JWTVerifier):
    """FastMCP's JWT verifier pinned to one Access application.

    JWTVerifier already does RS256-only, iss, aud, exp-if-present and the JWKS
    cache (1 h, refetched at once on an unknown ``kid``, which is how a
    Cloudflare key rotation lands; a failed fetch refuses the token). It does
    not require ``exp`` and ignores ``nbf``; both are added here.
    """

    # ponytail: an unknown kid refetches the JWKS every time (FastMCP has no
    # cooldown). Only cloudflared and the NetworkPolicy reach the origin; add a
    # cooldown if the origin is ever exposed another way.

    def __init__(self, config: AccessConfig, **kwargs: Any) -> None:
        super().__init__(
            jwks_uri=f"https://{config.team_domain}/cdn-cgi/access/certs",
            issuer=f"https://{config.team_domain}",
            audience=config.aud,
            algorithm="RS256",
            **kwargs,
        )

    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            return await super()._fetch_jwks()
        except Exception as exc:
            # Refuses everyone at once, so it is logged (a bad token is not:
            # anyone reaching the origin can send one).
            logger.warning("Cloudflare Access JWKS fetch failed: %s", exc)
            raise

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        claims = result.claims or {}
        if not isinstance(claims.get("exp"), int | float):
            return None
        nbf = claims.get("nbf")
        if nbf is not None and (
            not isinstance(nbf, int | float) or nbf > time.time() + CLOCK_TOLERANCE_S
        ):
            return None
        return result

    def get_middleware(self) -> list:
        return [
            Middleware(AccessHeaderAsBearer),
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(self)),
            Middleware(AuthContextMiddleware),
        ]


class AccessHeaderAsBearer:
    """Make ``Cf-Access-Jwt-Assertion`` the only bearer the auth layer sees.

    Any client ``Authorization`` header is dropped (Access may forward its own
    OAuth bearer, and a direct caller could put anything there). A repeated
    Access header is ambiguous and dropped too, which leaves no bearer: 401.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
            tokens = [v for k, v in headers if k.lower() == ACCESS_JWT_HEADER]
            if len(tokens) == 1 and tokens[0].strip():
                headers.append((b"authorization", b"Bearer " + tokens[0].strip()))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


_enabled = False


def enabled() -> bool:
    """Whether this process runs in Cloudflare Access mode."""
    return _enabled


def verified_email() -> str | None:
    """The person behind the current request, or None (service token, no token)."""
    token = get_access_token()
    email = (token.claims or {}).get("email") if token else None
    return email.strip() if isinstance(email, str) and "@" in email else None


def enable_from_env(
    env: Any, mcp: Any, transport_settings: Callable[[], tuple[str | None, bool]]
) -> AccessConfig | None:
    """Turn Access mode on for ``mcp`` when the env asks for it.

    ``transport_settings`` returns (transport, MCP_AUTH_TOKEN set). Exits on a
    half config, on stdio (no headers to verify) and next to MCP_AUTH_TOKEN
    (upstream main() would replace this auth with the bearer).
    """
    global _enabled
    try:
        config = read_config(env)
        if config is None:
            return None
        transport, bearer_set = transport_settings()
        if transport != "http":
            raise ValueError("Cloudflare Access mode needs TRANSPORT=http")
        if bearer_set:
            raise ValueError("Cloudflare Access mode cannot be combined with MCP_AUTH_TOKEN")
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201 - before logging configured
        sys.exit(1)
    mcp.auth = CloudflareAccessVerifier(config)
    _enabled = True
    print(  # noqa: T201 - same reason as write_tools' banner
        f"netbox-mcp-server: Cloudflare Access ENABLED (team {config.team_domain}): "
        "every request needs Cf-Access-Jwt-Assertion, writes need a person",
        file=sys.stderr,
    )
    return config
