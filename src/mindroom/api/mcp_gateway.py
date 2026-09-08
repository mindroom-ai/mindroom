"""Opt-in personal MCP gateway, OAuth endpoints, and signed browser consent."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sqlite3
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import HTTPException
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.settings import ClientRegistrationOptions
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mindroom.api.auth import require_personal_connections_user
from mindroom.api.config_lifecycle import app_state, rebind_current_request_snapshot, require_api_state
from mindroom.api.mcp_clients import client_routes
from mindroom.api.mcp_scim import scim_routes
from mindroom.api.personal_agent import PERSONAL_RESPONSE_HEADERS, resolve_personal_agent
from mindroom.logging_config import get_logger
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp_gateway.accounts import GatewayAccounts
from mindroom.mcp_gateway.admission import OnboardingRateLimiter
from mindroom.mcp_gateway.consent import render_consent_page
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider
from mindroom.mcp_gateway.server import GatewayServer, read_gateway_body, replay_gateway_body
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError
from mindroom.mcp_gateway.toolkits import drain_gateway_tool_cleanup
from mindroom.mcp_gateway.tools import get_tool, invoke_tool, search_tools
from mindroom.mcp_gateway.types import GatewayError, GatewayErrorCode, GatewayPrincipal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from starlette.types import Receive, Scope, Send

    from mindroom.api.personal_agent import PersonalAgentContext
    from mindroom.constants import RuntimePaths
    from mindroom.mcp_gateway.oauth import GatewayAccessToken
    from mindroom.mcp_gateway.types import GatewayToolResponse

logger = get_logger(__name__)
_MACHINE_HEADERS = {**PERSONAL_RESPONSE_HEADERS, "Access-Control-Allow-Origin": "*"}


def _enabled(paths: RuntimePaths) -> bool:
    return (
        paths.env_flag("MINDROOM_MCP_GATEWAY_ENABLED", default=False)
        and paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED", default=False)
        and paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT", default=False)
        and bool((paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip())
    )


def _browser_origins(paths: RuntimePaths) -> tuple[str, ...]:
    values = [
        paths.env_value("MINDROOM_PUBLIC_URL") or "",
        *(paths.env_value("MINDROOM_MCP_GATEWAY_ALLOWED_ORIGINS") or "").split(","),
    ]
    origins: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost"
        if parsed.hostname and not loopback:
            with suppress(ValueError):
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "#" in value
            or "?" in value
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
            or "*" in parsed.netloc
        ):
            msg = "MCP browser origins must be explicit HTTPS or loopback HTTP origins"
            raise ValueError(msg)
        _ = parsed.port
        origins.append(f"{parsed.scheme}://{parsed.netloc}")
    return tuple(dict.fromkeys(origins))


def gateway_cors_origins(paths: RuntimePaths, path: str) -> tuple[str, ...] | None:
    """Separate public OAuth and bearer MCP CORS from dashboard cookie policy."""
    if path == "/mcp/scim/v2" or path.startswith("/mcp/scim/v2/"):
        return ()
    if not _enabled(paths):
        return None
    if path in {
        "/mcp/oauth/register",
        "/mcp/oauth/token",
        "/mcp/oauth/revoke",
        "/.well-known/oauth-authorization-server/mcp/oauth",
        "/.well-known/oauth-protected-resource/mcp",
        "/mcp/oauth/.well-known/oauth-authorization-server",
    }:
        return ("*",)
    if path == "/mcp":
        try:
            return _browser_origins(paths)
        except ValueError:
            return ()
    return None


class GatewayRuntime:
    """Own client grants, lazy upstream sessions and one stateless MCP server."""

    def __init__(self, paths: RuntimePaths) -> None:
        self._onboarding_limiter = OnboardingRateLimiter(
            int(paths.env_value("MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT") or "60"),
            int(paths.env_value("MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT") or "10"),
        )
        self.provider = GatewayOAuthProvider(paths, public_url=paths.env_value("MINDROOM_PUBLIC_URL") or "")
        self.scim_token = self.provider.scim_token or ""
        self.accounts = GatewayAccounts(self.provider.store)
        self.manager = MCPServerManager(paths, validate_agent_function_names=False)
        self._config_lock = asyncio.Lock()
        self.server = GatewayServer(
            authenticate=self.authenticate,
            dispatch=self.dispatch,
            public_url=self.origin,
            allowed_origins=_browser_origins(paths),
            record_activity=self._record_activity,
            max_active_calls=int(paths.env_value("MINDROOM_MCP_GATEWAY_MAX_ACTIVE_CALLS") or "128"),
            max_user_calls=int(paths.env_value("MINDROOM_MCP_GATEWAY_MAX_USER_CALLS") or "32"),
            max_grant_calls=int(paths.env_value("MINDROOM_MCP_GATEWAY_MAX_GRANT_CALLS") or "16"),
        )
        authenticator = ClientAuthenticator(self.provider)
        self.authorize = AuthorizationHandler(self.provider)
        self.register = RegistrationHandler(
            self.provider,
            options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp:tools"],
                default_scopes=["mcp:tools"],
            ),
        )
        self.token = TokenHandler(self.provider, authenticator)
        self.revoke = RevocationHandler(self.provider, authenticator)

    @property
    def origin(self) -> str:
        """Return the configured public origin, independent of forwarded headers."""
        return self.provider.resource_url.removesuffix("/mcp")

    async def principal(self, request: Request) -> tuple[GatewayAccessToken, PersonalAgentContext]:
        """Resolve only a gateway bearer against current personal access policy."""
        value = request.headers.get("authorization", "")
        scheme, _, raw = value.partition(" ")
        token = (
            await self.provider.load_access_token(raw) if scheme.lower() == "bearer" and 0 < len(raw) <= 256 else None
        )
        headers = {
            "WWW-Authenticate": f'Bearer resource_metadata="{self.origin}/.well-known/oauth-protected-resource/mcp", scope="mcp:tools"',
        }
        if token is None:
            raise HTTPException(401, "Gateway bearer required", headers=headers)
        try:
            context = resolve_personal_agent(
                rebind_current_request_snapshot(request),
                token.authenticated_user_id,
                expected_agent_name=token.agent_name,
            )
        except HTTPException as exc:
            raise HTTPException(401, "Gateway grant is no longer authorized", headers=headers) from exc
        if context.requester_id != token.requester_id:
            raise HTTPException(401, "Gateway principal changed", headers=headers)
        return token, context

    async def authenticate(self, request: Request) -> GatewayPrincipal:
        """Authenticate every HTTP message, including discovery and cancellation."""
        token, _ = await self.principal(request)
        return GatewayPrincipal(grant_id=token.grant_id, requester_id=token.requester_id)

    async def _record_activity(self, request: Request) -> None:
        """Record successful use without turning completed provider actions into retryable failures."""
        _, _, raw = request.headers.get("authorization", "").partition(" ")
        try:
            token = await self.provider.load_access_token(raw)
            if token is not None:
                await self.provider.record_use(token)
        except sqlite3.Error as exc:
            logger.warning("mcp_gateway_activity_failed", error_type=type(exc).__name__)

    async def dispatch(self, request: Request, name: str, arguments: dict[str, Any]) -> GatewayToolResponse:
        """Recheck access immediately before selecting one tool operation."""
        _, context = await self.principal(request)
        async with self._config_lock:
            if not self.manager.is_configured_for(context.config):
                await self.manager.sync_servers(context.config, discover=False)
        if name == "search_tools":
            return await search_tools(context, manager=self.manager, **arguments)
        if name == "get_tool":
            return await get_tool(context, manager=self.manager, **arguments)
        if name == "invoke_tool":
            state = require_api_state(request.app)

            def require_current_config() -> None:
                with state.config_lock:
                    current = state.snapshot
                    if current.runtime_config != context.config or current.runtime_paths != context.runtime_paths:
                        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)

            return await invoke_tool(
                context,
                manager=self.manager,
                require_current_config=require_current_config,
                **arguments,
            )
        raise HTTPException(400, "Unknown gateway operation")


@asynccontextmanager
async def gateway_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the optional gateway without connecting any upstream integration."""
    state = app_state(app)
    paths = require_api_state(app).snapshot.runtime_paths
    if not _enabled(paths):
        yield
        return
    try:
        runtime = GatewayRuntime(paths)
    except ValueError:
        logger.warning("MCP gateway disabled: configure a valid public origin and limits")
        yield
        return
    state.mcp_gateway_runtime = runtime
    try:
        async with runtime.server.run():
            yield
    finally:
        state.mcp_gateway_runtime = None
        await drain_gateway_tool_cleanup()
        await runtime.manager.shutdown()


def _runtime(request: Request) -> GatewayRuntime:
    runtime = app_state(request.app).mcp_gateway_runtime
    paths = require_api_state(request.app).snapshot.runtime_paths
    if (
        runtime is None
        or not _enabled(paths)
        or (paths.env_value("MINDROOM_PUBLIC_URL") or "").rstrip("/") != runtime.origin
        or (paths.env_value("MINDROOM_MCP_SCIM_TOKEN") or "") != runtime.scim_token
    ):
        raise HTTPException(404, "MCP gateway is disabled", headers=PERSONAL_RESPONSE_HEADERS)
    return runtime


def _form(body: bytes) -> dict[str, str]:
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Invalid form") from exc
    if len(dict(pairs)) != len(pairs):
        raise HTTPException(400, "Repeated form fields are not supported")
    return dict(pairs)


def _require_form_content_type(request: Request) -> None:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "URL-encoded form required", headers=PERSONAL_RESPONSE_HEADERS)


def _onboarding_source(request: Request) -> str:
    """Return one canonical source bucket from the trusted ASGI peer address."""
    if request.client is None:
        return "unknown"
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.compressed


def _oauth_admission(request: Request, runtime: GatewayRuntime, operation: str) -> Response | None:
    """Answer preflight and reject excess public input before reading request bodies."""
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                **_MACHINE_HEADERS,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, MCP-Protocol-Version",
            },
        )
    if operation in {"register", "authorize"} and not runtime._onboarding_limiter.allow(_onboarding_source(request)):
        return JSONResponse(
            {"error": "slow_down"},
            status_code=429,
            headers={**_MACHINE_HEADERS, "Retry-After": "60"},
        )
    if len(request.scope.get("query_string", b"")) > 8192:
        return JSONResponse({"error": "invalid_request"}, status_code=413, headers=_MACHINE_HEADERS)
    return None


async def _oauth(request: Request) -> Response:
    runtime = _runtime(request)
    operation = request.scope["path"].rsplit("/", 1)[-1]
    admission = _oauth_admission(request, runtime, operation)
    if admission is not None:
        return admission
    try:
        if request.method == "POST":
            body = await read_gateway_body(request)
            if operation == "register":
                try:
                    json.loads(body)
                except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                    raise HTTPException(400, "Invalid registration JSON") from exc
            else:
                _require_form_content_type(request)
                fields = _form(body)
                if operation == "token" and fields.get("resource") != runtime.provider.resource_url:
                    return JSONResponse({"error": "invalid_target"}, status_code=400, headers=_MACHINE_HEADERS)
                if operation == "revoke" and "client_secret" not in fields:
                    # The SDK requires the field even for public clients authenticated with none.
                    body = urlencode({**fields, "client_secret": ""}).encode()
            request = Request(request.scope, replay_gateway_body(body, request.receive))
        handler = {
            "authorize": runtime.authorize,
            "register": runtime.register,
            "token": runtime.token,
            "revoke": runtime.revoke,
        }[operation]
        response = await handler.handle(request)
    except GatewayOAuthCapacityError:
        return JSONResponse(
            {"error": "temporarily_unavailable"},
            status_code=503,
            headers={**_MACHINE_HEADERS, "Retry-After": "60"},
        )
    except HTTPException as exc:
        return JSONResponse({"error": "invalid_request"}, status_code=exc.status_code, headers=_MACHINE_HEADERS)
    except TimeoutError:
        return JSONResponse({"error": "request_timeout"}, status_code=408, headers=_MACHINE_HEADERS)
    response.headers.update(PERSONAL_RESPONSE_HEADERS if operation == "authorize" else _MACHINE_HEADERS)
    return response


async def _metadata(request: Request) -> Response:
    runtime = _runtime(request)
    provider = runtime.provider
    if "oauth-protected-resource" in request.scope["path"]:
        payload = {
            "resource": provider.resource_url,
            "authorization_servers": [provider.issuer_url],
            "scopes_supported": ["mcp:tools"],
            "bearer_methods_supported": ["header"],
        }
    else:
        payload = {
            "issuer": provider.issuer_url,
            "authorization_endpoint": provider.issuer_url + "/authorize",
            "token_endpoint": provider.issuer_url + "/token",
            "registration_endpoint": provider.issuer_url + "/register",
            "revocation_endpoint": provider.issuer_url + "/revoke",
            "scopes_supported": ["mcp:tools"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }
    return JSONResponse(payload, headers=_MACHINE_HEADERS)


async def _consent(request: Request) -> Response:
    runtime = _runtime(request)
    user = await require_personal_connections_user(request)
    context = resolve_personal_agent(rebind_current_request_snapshot(request), user["matrix_user_id"], channel="matrix")
    account_id = None
    if runtime.provider.accounts_required:
        email = user.get("email")
        account_id = await runtime.accounts.resolve_active(email) if isinstance(email, str) else None
        if account_id is None:
            raise HTTPException(403, "An active provisioned account is required", headers=PERSONAL_RESPONSE_HEADERS)
    try:
        if request.method == "POST":
            if request.headers.get("origin") != runtime.origin or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Same-origin consent required", headers=PERSONAL_RESPONSE_HEADERS)
            _require_form_content_type(request)
            fields = _form(await read_gateway_body(request))
            if set(fields) != {"state", "csrf_token", "decision"} or fields["decision"] not in {"allow", "deny"}:
                raise HTTPException(400, "Invalid consent form", headers=PERSONAL_RESPONSE_HEADERS)
            redirect = await runtime.provider.finish_consent(
                fields["state"],
                requester_id=context.requester_id,
                authenticated_user_id=user["matrix_user_id"],
                agent_name=context.agent_name,
                account_id=account_id,
                csrf_token=fields["csrf_token"],
                allow=fields["decision"] == "allow",
            )
            return RedirectResponse(redirect, status_code=303, headers=PERSONAL_RESPONSE_HEADERS)
        state = request.query_params.get("state", "")
        if not state or len(state) > 256:
            raise HTTPException(400, "Invalid consent state", headers=PERSONAL_RESPONSE_HEADERS)
        consent = await runtime.provider.begin_consent(
            state,
            requester_id=context.requester_id,
            agent_name=context.agent_name,
            authenticated_user_id=user["matrix_user_id"],
            account_id=account_id,
        )
    except GatewayOAuthCapacityError:
        return JSONResponse(
            {"error": "temporarily_unavailable"},
            status_code=503,
            headers={**PERSONAL_RESPONSE_HEADERS, "Retry-After": "60"},
        )
    except AuthorizeError as exc:
        raise HTTPException(
            400,
            "Consent is invalid, expired, or belongs to another user",
            headers=PERSONAL_RESPONSE_HEADERS,
        ) from exc
    agent = context.config.get_agent(context.agent_name).display_name
    return HTMLResponse(
        render_consent_page(
            client_name=consent.client_name,
            agent_name=agent,
            redirect_uri=consent.redirect_uri,
            state=state,
            csrf_token=consent.csrf_token,
        ),
        headers={
            **PERSONAL_RESPONSE_HEADERS,
            # Native browser form POSTs send Origin:null under no-referrer.
            # Keep an origin for CSRF checks without exposing the consent URL.
            "Referrer-Policy": "strict-origin",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


class _MCPEndpoint:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            runtime = _runtime(Request(scope, receive))
        except HTTPException as exc:
            await JSONResponse(
                {"error": "gateway_disabled"},
                status_code=exc.status_code,
                headers=PERSONAL_RESPONSE_HEADERS,
            )(scope, receive, send)
            return
        await runtime.server(scope, receive, send)


def install_gateway_routes(app: FastAPI) -> None:
    """Register exact machine and browser routes before the frontend catch-all."""
    app.router.routes.extend(
        [
            *client_routes(_runtime),
            *scim_routes(_runtime),
            Route("/mcp", _MCPEndpoint(), methods=["GET", "POST", "DELETE"]),
            Route("/.well-known/oauth-authorization-server/mcp/oauth", _metadata, methods=["GET"]),
            Route("/mcp/oauth/.well-known/oauth-authorization-server", _metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource/mcp", _metadata, methods=["GET"]),
            Route("/mcp/oauth/authorize", _oauth, methods=["GET", "POST"]),
            Route("/mcp/oauth/register", _oauth, methods=["POST", "OPTIONS"]),
            Route("/mcp/oauth/token", _oauth, methods=["POST", "OPTIONS"]),
            Route("/mcp/oauth/revoke", _oauth, methods=["POST", "OPTIONS"]),
            Route("/connections/mcp/authorize", _consent, methods=["GET", "POST"]),
        ],
    )
