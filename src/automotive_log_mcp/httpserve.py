"""``serve --http``: the MCP server over streamable HTTP from the official SDK (D2, M6).

Same tools, call log and policy as over stdio, plus:

- **Bearer token.** Every request needs ``Authorization: Bearer <token>``; the token comes from
  ``AUTOMOTIVE_LOG_MCP_TOKEN`` or the file ``http.token_file`` names, and the server does not
  start without one. It is checked by the SDK's bearer middleware through
  :class:`StaticTokenVerifier` (constant-time comparison); a missing or wrong token gets 401. There
  is no OAuth: one token for whoever runs the server (multi-user operation is out of scope).
- **Path policy.** ``add_source`` paths are paths on the server's machine; over HTTP the policy's
  ``allowed_roots`` must list the directories it may read (none listed: every path is refused).
  Paths outside are refused before the server looks at them (see ``Ingestor.check``).
- **Host checks.** On a loopback address the SDK's DNS-rebinding protection is on; on another
  address ``http.allowed_hosts`` turns it on for the listed Host headers.

Plain HTTP: on an address other than loopback the token crosses the network in clear text; put
the server behind a TLS proxy or reach it through an SSH tunnel.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import sys

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .config import Config, ConfigError
from .policy import Policy
from .server import TOOL_NAMES, LogServer

ENV_TOKEN = "AUTOMOTIVE_LOG_MCP_TOKEN"
MIN_TOKEN = 16
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class StaticTokenVerifier:
    """The SDK's ``TokenVerifier`` for one shared bearer token."""

    def __init__(self, token: str) -> None:
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        given = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(given, self._digest):
            return None
        return AccessToken(token="<static>", client_id="automotive-log-mcp", scopes=[])


def load_token(config: Config) -> str:
    """The bearer token: ``AUTOMOTIVE_LOG_MCP_TOKEN``, else the contents of ``http.token_file``."""
    token = os.environ.get(ENV_TOKEN, "").strip()
    where = ENV_TOKEN
    if not token and config.http.token_file is not None:
        where = str(config.http.token_file)
        try:
            token = config.http.token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError(f"cannot read http.token_file {config.http.token_file}: {e}") from e
    if not token:
        raise ConfigError(
            f"serve --http needs a bearer token: set {ENV_TOKEN} or http.token_file in config.yaml "
            '(e.g. python -c "import secrets; print(secrets.token_urlsafe(32))" > token.txt)'
        )
    if len(token) < MIN_TOKEN or any(c.isspace() for c in token):
        raise ConfigError(f"the token from {where} must be at least {MIN_TOKEN} characters without spaces")
    return token


def http_policy(policy: Policy) -> Policy:
    """Over HTTP, no allowed_roots means no path at all (not every path, as over stdio)."""
    return policy if policy.allowed_roots is not None else dataclasses.replace(policy, allowed_roots=())


def build_http_app(
    config: Config, policy: Policy, token: str, session: str | None = None
) -> tuple[Starlette, Policy]:
    """The Starlette app serving MCP at ``http.path``, and the policy it enforces."""
    policy = http_policy(policy)
    policy.check_tools(TOOL_NAMES)
    h = config.http
    url_host = f"[{h.host}]" if ":" in h.host else h.host
    # No authorization server: issuer_url is required by the SDK's settings but never advertised
    # (no auth routes and no protected-resource metadata without resource_server_url).
    auth = AuthSettings(issuer_url=f"http://{url_host}:{h.port}", resource_server_url=None)  # type: ignore[arg-type]
    server = LogServer(config, policy, session, token_verifier=StaticTokenVerifier(token), auth=auth)
    security = None
    if h.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=list(h.allowed_hosts), allowed_origins=[]
        )
    app = server.mcp.streamable_http_app(
        streamable_http_path=h.path, transport_security=security, host=h.host
    )
    return app, policy


def serve_http(config: Config, policy: Policy) -> None:
    import uvicorn

    token = load_token(config)
    app, policy = build_http_app(config, policy, token)
    h = config.http
    roots = policy.roots_text()
    print(
        f"automotive-log-mcp: MCP over streamable HTTP at http://{h.host}:{h.port}{h.path} "
        f"(bearer token required; add_source reads from: {roots})",
        file=sys.stderr,
    )
    if h.host not in LOOPBACK:
        print(
            "automotive-log-mcp: warning: plain HTTP on a non-loopback address sends the token in clear "
            "text; use a TLS proxy or an SSH tunnel",
            file=sys.stderr,
        )
    uvicorn.run(app, host=h.host, port=h.port, log_level="warning")
