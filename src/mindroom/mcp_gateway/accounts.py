"""Bounded provisioned identities, separate from login and tool permissions.

Administrator-managed records are outside OAuth logical byte budgets. Deployments
must bound physical storage with the runtime volume quota.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.mcp_gateway.store import GatewayOAuthStore


class AccountConflictError(Exception):
    """The exact provisioned user name already exists."""


class AccountNotFoundError(Exception):
    """The immutable account identifier does not exist."""


class AccountValidationError(Exception):
    """A supported account field is invalid; never include submitted values."""


def migrate_accounts(connection: sqlite3.Connection) -> None:
    """Create the directory inside the caller's migration transaction."""
    connection.execute("""CREATE TABLE IF NOT EXISTS gateway_accounts (
        account_id TEXT PRIMARY KEY, user_name TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0, 1)),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        profile TEXT NOT NULL DEFAULT '{}'
    )""")


def account_is_active(connection: sqlite3.Connection, account_id: str | None) -> bool:
    """Require a currently active immutable account, with no status cache."""
    return (
        account_id is not None
        and connection.execute(
            "SELECT 1 FROM gateway_accounts WHERE account_id = ? AND active = 1",
            (account_id,),
        ).fetchone()
        is not None
    )


def _text(value: object, *, limit: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise AccountValidationError
    return value


def canonical_fields(value: object) -> dict[str, Any]:
    """Fold SCIM attribute names while rejecting ambiguous duplicate casing."""
    if not isinstance(value, dict):
        raise AccountValidationError
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.lower() in result:
            raise AccountValidationError
        result[key.lower()] = item
    return result


def _validate_account(value: object) -> dict[str, Any]:
    """Retain only bounded supported User fields, never password or extensions."""
    fields = canonical_fields(value)
    user_name = _text(fields.get("username"), limit=320)
    if not user_name or user_name != user_name.strip() or "@" not in user_name:
        raise AccountValidationError
    active = fields.get("active")
    if not isinstance(active, bool):
        raise AccountValidationError
    result: dict[str, Any] = {"userName": user_name, "active": active}
    for name in ("displayName", "externalId"):
        if name.lower() in fields:
            result[name] = _text(fields[name.lower()])
    if "name" in fields:
        names = canonical_fields(fields["name"])
        result["name"] = {
            name: _text(names[name.lower()])
            for name in ("givenName", "familyName", "formatted")
            if name.lower() in names
        }
    if "emails" in fields:
        result["emails"] = _emails(fields["emails"])

    return result


def _emails(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 10:
        raise AccountValidationError
    result = []
    for email in value:
        item = canonical_fields(email)
        clean: dict[str, Any] = {"value": _text(item.get("value"), limit=320)}
        if "type" in item:
            clean["type"] = _text(item["type"], limit=64)
        if "primary" in item:
            if not isinstance(item["primary"], bool):
                raise AccountValidationError
            clean["primary"] = item["primary"]
        result.append(clean)
    if sum(item.get("primary", False) for item in result) > 1:
        raise AccountValidationError
    return result


def _resource(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **json.loads(row["profile"]),
        "id": row["account_id"],
        "userName": row["user_name"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class GatewayAccounts:
    """Read and mutate provisioned accounts in the OAuth transaction boundary."""

    def __init__(self, store: GatewayOAuthStore) -> None:
        self.store = store

    async def resolve_active(self, user_name: str) -> str | None:
        """Match the exact verified login email; never normalize identities."""

        def read(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                "SELECT account_id FROM gateway_accounts WHERE user_name = ? AND active = 1",
                (user_name,),
            ).fetchone()
            return row["account_id"] if row else None

        return await self.store.read(read)

    async def create(self, value: object) -> dict[str, Any]:
        """Create a fresh immutable identity; recreated users get new identifiers."""
        clean = _validate_account(value)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            now = time.time()
            account_id = str(uuid.uuid4())
            try:
                connection.execute(
                    "INSERT INTO gateway_accounts VALUES (?, ?, ?, ?, ?, ?)",
                    (account_id, clean["userName"], int(clean["active"]), now, now, json.dumps(clean)),
                )
            except sqlite3.IntegrityError as error:
                raise AccountConflictError from error
            return self._get(connection, account_id)

        return await self.store.transact(write)

    @staticmethod
    def _get(connection: sqlite3.Connection, account_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM gateway_accounts WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountNotFoundError
        return _resource(row)

    async def get(self, account_id: str) -> dict[str, Any]:
        """Read one account without extending any authorization lifetime."""
        return await self.store.read(lambda connection: self._get(connection, account_id))

    async def list_accounts(
        self,
        *,
        start_index: int = 1,
        count: int = 100,
        attribute: str | None = None,
        value: str | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return one bounded, stable page and the full matching count."""
        clauses = {
            "username": "user_name = ?",
            "id": "account_id = ?",
            "emails.value": "EXISTS (SELECT 1 FROM json_each(profile, '$.emails') WHERE json_extract(value, '$.value') = ?)",
        }
        if start_index > 2**63 - 1 or (attribute is not None and attribute not in clauses):
            raise AccountValidationError
        if value is not None:
            _text(value, limit=2048)
        where = " WHERE " + clauses[attribute] if attribute is not None else ""
        parameters = (value,) if attribute is not None else ()

        def read(connection: sqlite3.Connection) -> tuple[int, list[dict[str, Any]]]:
            total = connection.execute("SELECT COUNT(*) FROM gateway_accounts" + where, parameters).fetchone()[0]  # noqa: S608
            rows = connection.execute(
                "SELECT * FROM gateway_accounts" + where + " ORDER BY account_id LIMIT ? OFFSET ?",  # noqa: S608
                (*parameters, min(max(count, 0), 100), max(start_index - 1, 0)),
            ).fetchall()
            return total, [_resource(row) for row in rows]

        return await self.store.read(read)

    def _invalidate(self, connection: sqlite3.Connection, account_id: str) -> None:
        for row in connection.execute("SELECT grant_id FROM grants WHERE account_id = ?", (account_id,)).fetchall():
            self.store.delete_family(connection, row["grant_id"])
        connection.execute("DELETE FROM pending WHERE account_id = ?", (account_id,))

    async def replace(self, account_id: str, value: object) -> dict[str, Any]:
        """Replace mutable attributes and invalidate access on rename or disable."""
        return await self.update(account_id, [lambda _: _validate_account(value)])

    async def update(self, account_id: str, transforms: Sequence[Callable[[dict[str, Any]], object]]) -> dict[str, Any]:
        """Validate and apply all PATCH operations inside one atomic transaction."""

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            old = self._get(connection, account_id)
            for transform in transforms:
                clean = _validate_account(transform(dict(old)))
                if clean == _validate_account(old):
                    continue
                try:
                    connection.execute(
                        "UPDATE gateway_accounts SET user_name = ?, active = ?, profile = ?, "
                        "updated_at = MAX(updated_at, ?) WHERE account_id = ?",
                        (clean["userName"], int(clean["active"]), json.dumps(clean), time.time(), account_id),
                    )
                except sqlite3.IntegrityError as error:
                    raise AccountConflictError from error
                if old["userName"] != clean["userName"] or not clean["active"]:
                    self._invalidate(connection, account_id)
                old = clean
            return self._get(connection, account_id)

        return await self.store.transact(write)

    async def delete(self, account_id: str) -> None:
        """Delete the account and all bound consent and grants together."""

        def write(connection: sqlite3.Connection) -> None:
            self._get(connection, account_id)
            self._invalidate(connection, account_id)
            connection.execute("DELETE FROM gateway_accounts WHERE account_id = ?", (account_id,))

        await self.store.transact(write)
