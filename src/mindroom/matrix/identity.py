"""Unified Matrix ID handling system."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from mindroom.matrix.state import matrix_state_for_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_CURRENT_USER_LOCALPART_PATTERN = re.compile(r"^[a-z0-9._=/+-]+$")
_SERVER_DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,63}$")
_SERVER_IPV6_LITERAL_PATTERN = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
# Greedy matching returns the longest run of complete DNS labels, which is the longest host a prefix can end with.
_SERVER_HOST_PREFIX_PATTERN = re.compile(r"(?:[A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}")
_MAX_PORT_DIGITS = 5
MAX_MATRIX_USER_ID_BYTES = 255

__all__ = [
    "MAX_MATRIX_USER_ID_BYTES",
    "MatrixID",
    "managed_account_key",
    "managed_account_user_id",
    "matrix_user_id_from_email",
    "matrix_user_id_prefix_candidates",
    "parse_current_matrix_user_id",
    "parse_historical_matrix_user_id",
    "try_parse_historical_matrix_user_id",
    "valid_matrix_server_name",
    "validate_email_to_matrix_mapping",
]


@dataclass(frozen=True)
class MatrixID:
    """Immutable Matrix ID representation with parsing and validation."""

    username: str
    domain: str

    @classmethod
    def parse(cls, matrix_id: str) -> MatrixID:
        """Parse a Matrix ID like @alice:example.com."""
        return _parse_matrix_id(matrix_id)

    @classmethod
    def from_username(cls, username: str, domain: str) -> MatrixID:
        """Create a MatrixID from a username (without @ prefix)."""
        return cls(username=username, domain=domain)

    @property
    def full_id(self) -> str:
        """Get the full Matrix ID like @alice:example.com."""
        return f"@{self.username}:{self.domain}"

    def __str__(self) -> str:
        """Return the full Matrix ID string representation."""
        return self.full_id


@dataclass(frozen=True)
class _ThreadStateKey:
    """Represents a thread state key like 'thread_id:agent_name'."""

    thread_id: str
    agent_name: str

    @classmethod
    def parse(cls, state_key: str) -> _ThreadStateKey:
        """Parse a state key."""
        parts = state_key.split(":", 1)
        if len(parts) != 2:
            msg = f"Invalid state key: {state_key}"
            raise ValueError(msg)
        return cls(thread_id=parts[0], agent_name=parts[1])

    @property
    def key(self) -> str:
        """Get the full state key."""
        return f"{self.thread_id}:{self.agent_name}"

    def __str__(self) -> str:
        """Return the state key string representation."""
        return self.key


@lru_cache(maxsize=512)
def _parse_matrix_id(matrix_id: str) -> MatrixID:
    """Cached structural Matrix user ID parser.

    Matrix-originated event and member data can contain historical user IDs whose
    localparts do not match the current creation grammar, so this parser stays
    tolerant and leaves strict validation to explicit auth-boundary helpers.
    """
    if not matrix_id.startswith("@"):
        msg = f"Invalid Matrix ID: {matrix_id}"
        raise ValueError(msg)
    if ":" not in matrix_id:
        msg = f"Invalid Matrix ID, missing domain: {matrix_id}"
        raise ValueError(msg)
    parts = matrix_id[1:].split(":", 1)
    if len(parts) != 2:
        msg = f"Invalid Matrix ID format: {matrix_id}"
        raise ValueError(msg)

    username, domain = parts
    if "\x00" in username:
        msg = f"Invalid Matrix ID localpart: {matrix_id}"
        raise ValueError(msg)

    return MatrixID(username=username, domain=domain)


def parse_current_matrix_user_id(matrix_id: str) -> str:
    """Return a canonical current-grammar Matrix user ID, or raise ValueError."""
    parsed = MatrixID.parse(matrix_id)
    if not _CURRENT_USER_LOCALPART_PATTERN.fullmatch(parsed.username):
        msg = f"Invalid Matrix ID localpart: {matrix_id}"
        raise ValueError(msg)
    _validate_matrix_user_id_common(parsed, matrix_id)
    return parsed.full_id


def parse_historical_matrix_user_id(matrix_id: str) -> str:
    """Return a canonical Matrix user ID while accepting historical localparts."""
    parsed = MatrixID.parse(matrix_id)
    _validate_matrix_user_id_common(parsed, matrix_id)
    return parsed.full_id


def try_parse_historical_matrix_user_id(value: str | None) -> str | None:
    """Return a canonical Matrix user ID when a nullable value parses."""
    if value is None:
        return None
    try:
        return parse_historical_matrix_user_id(value)
    except ValueError:
        return None


def validate_email_to_matrix_mapping(template: str, email_domain: str | None) -> None:
    """Require an explicit email domain and an unambiguous Matrix identity template."""
    if (
        template.count("{localpart}") != 1
        or "{" in template.replace("{localpart}", "")
        or "}" in template.replace("{localpart}", "")
        or try_parse_historical_matrix_user_id(template.replace("{localpart}", "example")) is None
        or email_domain is None
        or re.fullmatch(r"[A-Za-z0-9.-]+", email_domain) is None
    ):
        msg = "Email mapping requires a valid Matrix template and explicit email domain"
        raise ValueError(msg)


def matrix_user_id_from_email(email: str, template: str, email_domain: str | None) -> str:
    """Map an email from the configured domain to its canonical Matrix identity."""
    localpart, separator, domain = email.partition("@")
    if (
        not separator
        or not localpart
        or any(char.isspace() for char in email)
        or email_domain is None
        or domain.lower() != email_domain.lower()
    ):
        msg = "Email does not match the configured identity domain"
        raise ValueError(msg)
    return parse_historical_matrix_user_id(template.replace("{localpart}", localpart))


def matrix_user_id_prefix_candidates(token: str) -> list[str]:
    """Return, longest first, every prefix of an ``@`` token that could be a valid Matrix user ID.

    A user ID is ``@localpart:server_name``, and a server name is a DNS host or a bracketed IPv6 literal,
    optionally followed by a port of at most five digits.
    Its localpart ends at the first colon, so only the longest host and its few port lengths can pass validation.
    Callers still validate each candidate; this only avoids validating every prefix of a long token.
    """
    token = token[:MAX_MATRIX_USER_ID_BYTES]
    head, separator, server_part = token.partition(":")
    if not separator:
        return []
    if server_part.startswith("["):
        host = server_part[: server_part.find("]") + 1]
    else:
        host_match = _SERVER_HOST_PREFIX_PATTERN.match(server_part)
        host = "" if host_match is None else host_match.group(0)
    if not host:
        return []
    user_id_with_host = f"{head}:{host}"
    remainder = server_part[len(host) :]
    if not remainder.startswith(":"):
        return [user_id_with_host]
    port_lengths = range(min(len(remainder) - 1, _MAX_PORT_DIGITS), 0, -1)
    return [*(user_id_with_host + remainder[: length + 1] for length in port_lengths), user_id_with_host]


def _validate_matrix_user_id_common(parsed: MatrixID, matrix_id: str) -> None:
    # Every character encodes to at least one byte, so overlong IDs are rejected before the surrogate and server checks.
    if len(matrix_id) > MAX_MATRIX_USER_ID_BYTES:
        msg = f"Invalid Matrix ID length: {matrix_id}"
        raise ValueError(msg)
    if _contains_surrogate(parsed.username):
        msg = f"Invalid Matrix ID localpart: {matrix_id}"
        raise ValueError(msg)
    if not valid_matrix_server_name(parsed.domain):
        msg = f"Invalid Matrix ID server name: {matrix_id}"
        raise ValueError(msg)
    try:
        encoded_matrix_id = matrix_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        msg = f"Invalid Matrix ID: {matrix_id}"
        raise ValueError(msg) from exc
    if len(encoded_matrix_id) > MAX_MATRIX_USER_ID_BYTES:
        msg = f"Invalid Matrix ID length: {matrix_id}"
        raise ValueError(msg)


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def valid_matrix_server_name(server_name: str) -> bool:
    """Return whether a value matches the Matrix server_name grammar."""
    if not server_name:
        return False

    if server_name.startswith("["):
        return _valid_bracketed_ipv6_server_name(server_name)

    if ":" in server_name:
        host, port = server_name.rsplit(":", 1)
        return _valid_unbracketed_server_host(host) and _valid_port(port)

    return _valid_unbracketed_server_host(server_name)


def _valid_unbracketed_server_host(host: str) -> bool:
    if not host or len(host) > 255:
        return False
    return all(_SERVER_DNS_LABEL_PATTERN.fullmatch(label) is not None for label in host.split("."))


def _valid_bracketed_ipv6_server_name(server_name: str) -> bool:
    host, port = _split_bracketed_server_name(server_name)
    if host is None or not _valid_port(port):
        return False
    ipv6_literal = host[1:-1]
    if _SERVER_IPV6_LITERAL_PATTERN.fullmatch(ipv6_literal) is None:
        return False
    try:
        ipaddress.IPv6Address(ipv6_literal)
    except ValueError:
        return False
    return True


def _split_bracketed_server_name(server_name: str) -> tuple[str | None, str | None]:
    closing_bracket_index = server_name.find("]")
    if closing_bracket_index == -1:
        return None, None
    host = server_name[: closing_bracket_index + 1]
    remainder = server_name[closing_bracket_index + 1 :]
    if not remainder:
        return host, None
    if not remainder.startswith(":"):
        return None, None
    return host, remainder[1:]


def _valid_port(port: str | None) -> bool:
    return port is None or (1 <= len(port) <= 5 and port.isdecimal() and int(port) <= 65535)


def managed_account_key(entity_name: str) -> str:
    """Return the Matrix state account key for a managed agent-like entity."""
    return f"agent_{entity_name}"


def managed_account_user_id(account_key: str, domain: str, runtime_paths: RuntimePaths) -> str | None:
    """Return a persisted managed account's full Matrix user ID."""
    account = matrix_state_for_runtime(runtime_paths).get_account(account_key)
    if account is None:
        return None
    return MatrixID.from_username(account.username, account.domain or domain).full_id
