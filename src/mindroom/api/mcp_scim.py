"""Opt-in SCIM User provisioning with independent bearer authentication."""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Never, Protocol

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mindroom.mcp_gateway.accounts import (
    AccountConflictError,
    AccountNotFoundError,
    AccountValidationError,
    canonical_fields,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import Request

    from mindroom.mcp_gateway.accounts import GatewayAccounts

_BASE = "/mcp/scim/v2"
_CORE = "urn:ietf:params:scim:schemas:core:2.0:"
_MESSAGES = "urn:ietf:params:scim:api:messages:2.0:"
_USER_SCHEMA = _CORE + "User"
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_MAX_BODY = 65_536
_FILTER = re.compile(r'\s*(userName|emails\.value|id)\s+eq\s+("(?:[^"\\]|\\.)*")\s*', re.IGNORECASE)
_FIELDS = {"username", "active", "displayname", "externalid", "name", "emails"}


class _ScimProvider(Protocol):
    """Provisioning only needs the provider's managed-account enable flag."""

    @property
    def accounts_required(self) -> bool:
        """Whether provisioned identities are required."""
        ...


class _ScimRuntime(Protocol):
    """Narrow runtime contract, avoiding imports from gateway route assembly."""

    @property
    def provider(self) -> _ScimProvider:
        """Account-aware authorization provider."""
        ...

    @property
    def accounts(self) -> GatewayAccounts:
        """Provisioned account directory owned by the gateway runtime."""
        ...

    @property
    def origin(self) -> str:
        """Configured public origin for resource locations."""
        ...

    @property
    def scim_token(self) -> str | None:
        """Dedicated provisioning credential."""
        ...


class _ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def _response(value: object, status: int = 200, *, location: str | None = None) -> JSONResponse:
    headers = dict(_HEADERS)
    if location:
        headers["Location"] = location
    return JSONResponse(value, status_code=status, headers=headers, media_type="application/scim+json")


def _error(error: _ScimError) -> JSONResponse:
    value = {"schemas": [_MESSAGES + "Error"], "status": str(error.status), "detail": error.detail}
    if error.scim_type:
        value["scimType"] = error.scim_type
    response = _response(value, error.status)
    if error.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _user(account: dict[str, Any], origin: str) -> dict[str, Any]:
    resource = {key: value for key, value in account.items() if key not in {"created_at", "updated_at"}}
    resource["schemas"] = [_USER_SCHEMA]
    resource["meta"] = {
        "resourceType": "User",
        "location": origin + _BASE + "/Users/" + account["id"],
        "created": datetime.fromtimestamp(account["created_at"], UTC).isoformat(),
        "lastModified": datetime.fromtimestamp(account["updated_at"], UTC).isoformat(),
    }
    return resource


def _list(resources: list[dict[str, Any]], *, total: int | None = None, start: int = 1) -> dict[str, Any]:
    return {
        "schemas": [_MESSAGES + "ListResponse"],
        "totalResults": len(resources) if total is None else total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AccountValidationError
        result[key] = value
    return result


def _reject_constant(_value: str) -> Never:
    raise AccountValidationError


async def _body(request: Request, schema: str) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
        "application/json",
        "application/scim+json",
    }:
        raise _ScimError(415, "Use application/scim+json.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY:
            raise _ScimError(413, "Request body exceeds the supported limit.", "tooLarge")
        body.extend(chunk)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
        fields = canonical_fields(value)
    except (ValueError, RecursionError) as error:
        raise AccountValidationError from error
    if fields.get("schemas") != [schema]:
        raise AccountValidationError
    return fields


def _pagination(request: Request) -> tuple[int, int, str | None, str | None]:
    parameters = request.query_params
    if len(parameters.multi_items()) != len(parameters) or set(parameters) - {"startIndex", "count", "filter"}:
        raise _ScimError(400, "Unsupported query parameters.", "invalidFilter")
    try:
        start = int(parameters.get("startIndex", "1"))
        count = int(parameters.get("count", "100"))
    except ValueError as error:
        raise _ScimError(400, "Invalid pagination.", "invalidValue") from error
    if count < 0 or start > 2**63 - 1:
        raise _ScimError(400, "Invalid pagination.", "invalidValue")
    attribute = value = None
    if "filter" in parameters:
        raw_filter = parameters["filter"]
        match = _FILTER.fullmatch(raw_filter) if len(raw_filter) <= 2048 else None
        if match is None:
            raise _ScimError(400, "Only exact userName, emails.value or id equality is supported.", "invalidFilter")
        attribute = match[1].lower()
        try:
            value = json.loads(match[2])
        except ValueError as error:
            raise _ScimError(400, "Invalid filter value.", "invalidFilter") from error
    return max(start, 1), min(count, 100), attribute, value


def _patch_operations(body: dict[str, Any]) -> list[dict[str, Any]]:
    operations = body.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise AccountValidationError
    return [canonical_fields(operation) for operation in operations]


def _add_emails(existing: list[dict[str, Any]], incoming: Sequence[object]) -> list[dict[str, Any]]:
    emails = [canonical_fields(email) for email in existing]
    for raw in incoming:
        fields = canonical_fields(raw)
        email = {key: value for key, value in fields.items() if key in {"value", "type", "primary"}}
        target = next(
            (
                item
                for item in emails
                if item.get("value") == email.get("value") and item.get("type") == email.get("type")
            ),
            None,
        )
        if target is None:
            target = email
            emails.append(target)
            if len(emails) > 10:
                raise AccountValidationError
        else:
            target.update(email)
        if email.get("primary") is True:
            for item in emails:
                if item is not target and item.get("primary") is True:
                    item["primary"] = False
    return emails


def _change(resource: dict[str, Any], name: str, value: object, verb: str) -> None:
    if name not in _FIELDS:
        raise _ScimError(400, "Unsupported attribute path.", "invalidPath")
    if verb == "remove":
        if name in {"username", "active"}:
            raise _ScimError(400, "Required account attributes cannot be removed.", "mutability")
        if name not in resource:
            raise _ScimError(400, "No matching attribute.", "noTarget")
        del resource[name]
    elif verb == "add" and name == "emails" and isinstance(value, list):
        resource[name] = _add_emails(resource.get(name, []), value)
    elif verb == "add" and name == "name" and isinstance(value, dict):
        resource[name] = {**canonical_fields(resource.get(name, {})), **canonical_fields(value)}
    else:
        resource[name] = value


def _name_patch(resource: dict[str, Any], path: str, value: object, verb: str) -> None:
    name = path.removeprefix("name.")
    if name not in {"givenname", "familyname", "formatted"}:
        raise _ScimError(400, "Unsupported attribute path.", "invalidPath")
    names = canonical_fields(resource.get("name", {}))
    if verb == "remove":
        if name not in names:
            raise _ScimError(400, "No matching attribute.", "noTarget")
        del names[name]
    else:
        names[name] = value
    resource["name"] = names


def _apply_patch(operation: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    resource = canonical_fields(account)
    verb = operation.get("op")
    if not isinstance(verb, str) or verb.lower() not in {"add", "replace", "remove"}:
        raise _ScimError(400, "Unsupported PATCH operation.", "invalidSyntax")
    verb = verb.lower()
    path = operation.get("path")
    if isinstance(path, str) and path.lower().startswith("name."):
        _name_patch(resource, path.lower(), operation.get("value"), verb)
        return resource
    if path is None and verb != "remove":
        changes = canonical_fields(operation.get("value"))
    elif isinstance(path, str) and path.lower() in _FIELDS:
        changes = {path.lower(): operation.get("value")}
    else:
        raise _ScimError(400, "Unsupported attribute path.", "invalidPath")
    for name, value in changes.items():
        _change(resource, name, value, verb)
    return resource


def _discovery(path: str, origin: str) -> dict[str, Any] | None:
    if path == "/ServiceProviderConfig":
        return {
            "schemas": [_CORE + "ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 100},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Provisioning bearer",
                    "description": "Dedicated provisioning credential; browser and MCP credentials are not accepted.",
                },
            ],
        }
    resource_type = {
        "schemas": [_CORE + "ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": _USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": origin + _BASE + "/ResourceTypes/User"},
    }
    attributes = []
    for name, kind in [
        ("userName", "string"),
        ("active", "boolean"),
        ("displayName", "string"),
        ("externalId", "string"),
        ("name", "complex"),
        ("emails", "complex"),
    ]:
        attribute: dict[str, Any] = {
            "name": name,
            "type": kind,
            "multiValued": name == "emails",
            "required": name in {"userName", "active"},
            "mutability": "readWrite",
            "returned": "default",
            "uniqueness": "server" if name == "userName" else "none",
        }
        if kind == "string":
            attribute["caseExact"] = True
        if kind == "complex":
            children = (
                [("value", "string"), ("type", "string"), ("primary", "boolean")]
                if name == "emails"
                else [("givenName", "string"), ("familyName", "string"), ("formatted", "string")]
            )
            attribute["subAttributes"] = [
                {
                    "name": child,
                    "type": child_type,
                    **({"caseExact": True} if child_type == "string" else {}),
                    "multiValued": False,
                    "required": child == "value",
                    "mutability": "readWrite",
                    "returned": "default",
                    "uniqueness": "none",
                }
                for child, child_type in children
            ]
        attributes.append(attribute)
    schema = {
        "schemas": [_CORE + "Schema"],
        "id": _USER_SCHEMA,
        "name": "User",
        "attributes": attributes,
        "description": "MCP provisioning profile. userName must exactly match the verified login email.",
    }
    return {
        "/ResourceTypes": _list([resource_type]),
        "/ResourceTypes/User": resource_type,
        "/Schemas": _list([schema]),
        "/Schemas/" + _USER_SCHEMA: schema,
    }.get(path)


def _authenticate(request: Request, runtime: _ScimRuntime) -> None:
    secret = runtime.scim_token
    if not runtime.provider.accounts_required or not secret:
        raise _ScimError(404, "Provisioning is not enabled.")
    headers = request.headers.getlist("authorization")
    parts = headers[0].split(" ") if len(headers) == 1 and len(headers[0]) <= 4096 else []
    supplied = parts[1].encode("utf-8") if len(parts) == 2 and parts[0].lower() == "bearer" else b""
    if not hmac.compare_digest(supplied, secret.encode("utf-8")):
        raise _ScimError(401, "A valid provisioning bearer credential is required.")


async def _users(request: Request, runtime: _ScimRuntime) -> Response:
    if request.method == "GET":
        start, count, attribute, value = _pagination(request)
        total, accounts = await runtime.accounts.list_accounts(
            start_index=start,
            count=count,
            attribute=attribute,
            value=value,
        )
        return _response(_list([_user(account, runtime.origin) for account in accounts], total=total, start=start))
    if request.method == "POST":
        account = await runtime.accounts.create(await _body(request, _USER_SCHEMA))
        user = _user(account, runtime.origin)
        return _response(user, 201, location=user["meta"]["location"])
    raise _ScimError(405, "Method not supported.")


async def _one_user(request: Request, runtime: _ScimRuntime, account_id: str) -> Response:
    if request.method == "GET":
        account = await runtime.accounts.get(account_id)
    elif request.method == "PUT":
        account = await runtime.accounts.replace(account_id, await _body(request, _USER_SCHEMA))
    elif request.method == "PATCH":
        operations = _patch_operations(await _body(request, _MESSAGES + "PatchOp"))
        account = await runtime.accounts.update(
            account_id,
            [partial(_apply_patch, operation) for operation in operations],
        )
    elif request.method == "DELETE":
        await runtime.accounts.delete(account_id)
        return Response(status_code=204, headers=_HEADERS)
    else:
        raise _ScimError(405, "Method not supported.")
    return _response(_user(account, runtime.origin))


async def _dispatch(request: Request, runtime: _ScimRuntime) -> Response:
    _authenticate(request, runtime)
    if len(request.scope.get("query_string", b"")) > 4096:
        raise _ScimError(400, "Query exceeds the supported limit.", "invalidFilter")
    path = request.url.path.removeprefix(_BASE).rstrip("/")
    if request.query_params and (path != "/Users" or request.method != "GET"):
        raise _ScimError(400, "Unsupported query parameters.", "invalidFilter")
    if path == "/Groups" or path.startswith("/Groups/"):
        raise _ScimError(501, "Groups are not supported. Disable group management in the connector.")
    discovery = _discovery(path, runtime.origin)
    if discovery is not None:
        if request.method != "GET":
            raise _ScimError(405, "Method not supported.")
        return _response(discovery)
    if path == "/Users":
        return await _users(request, runtime)
    if path.startswith("/Users/") and "/" not in path[len("/Users/") :]:
        return await _one_user(request, runtime, path[len("/Users/") :])
    raise _ScimError(404, "Unknown provisioning resource.")


def scim_routes(runtime_for_request: Callable[[Request], _ScimRuntime]) -> list[Route]:
    """Register SCIM before the gateway catch-all; no browser or CORS fallback."""

    async def handle(request: Request) -> Response:
        try:
            return await _dispatch(request, runtime_for_request(request))
        except AccountConflictError:
            return _error(_ScimError(409, "The userName already exists.", "uniqueness"))
        except AccountNotFoundError:
            return _error(_ScimError(404, "Account not found."))
        except AccountValidationError:
            return _error(_ScimError(400, "Invalid or missing supported account attributes.", "invalidValue"))
        except _ScimError as error:
            return _error(error)

    return [Route(_BASE + "/{path:path}", handle, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])]
