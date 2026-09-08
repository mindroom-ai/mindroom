"""Typed identity and response contracts for the personal gateway."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NotRequired, TypedDict


@dataclass(frozen=True)
class GatewayPrincipal:
    """Validated grant and authoritative requester for admission and cancellation."""

    grant_id: str
    requester_id: str


class GatewayErrorCode(StrEnum):
    """Error codes emitted by gateway tools and transport."""

    CONNECTION_REQUIRED = "connection_required"
    TOOL_NOT_FOUND = "tool_not_found"
    APPROVAL_REQUIRED = "approval_required"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    RESULT_TOO_LARGE = "result_too_large"
    SCHEMA_TOO_LARGE = "schema_too_large"
    UNAUTHORIZED = "unauthorized"
    DUPLICATE_REQUEST = "duplicate_request"
    BUSY = "busy"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class GatewayErrorDetail(TypedDict):
    """Safe error detail returned to a gateway client."""

    code: GatewayErrorCode
    message: str
    connection_url: NotRequired[str]


class GatewayErrorResponse(TypedDict):
    """Gateway error envelope."""

    error: GatewayErrorDetail


class SearchItem(TypedDict):
    """One toolkit or function returned by gateway search."""

    toolkit: str
    function: NotRequired[str]
    description: str
    next: NotRequired[str]


class SearchResponse(TypedDict):
    """Successful gateway search envelope."""

    results: list[SearchItem]


class ToolSchemaResponse(TypedDict):
    """Successful gateway schema response."""

    toolkit: str
    function: str
    description: str
    inputSchema: dict[str, Any]


class InvocationResponse(TypedDict):
    """Successful gateway invocation envelope."""

    result: Any


type GatewaySuccessResponse = SearchResponse | ToolSchemaResponse | InvocationResponse
type _GatewayResult[T] = T | GatewayErrorResponse
type SearchResult = _GatewayResult[SearchResponse]
type ToolSchemaResult = _GatewayResult[ToolSchemaResponse]
type InvocationResult = _GatewayResult[InvocationResponse]
type GatewayToolResponse = _GatewayResult[GatewaySuccessResponse]


class GatewayError(Exception):
    """Carry a safe typed error code across gateway module boundaries."""

    def __init__(self, code: GatewayErrorCode) -> None:
        self.code = code
        super().__init__(code)
