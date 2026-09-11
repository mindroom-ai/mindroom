"""Durable agent selections shared by every client of one verified owner."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.mcp_gateway.accounts import GatewayAccounts
from mindroom.mcp_gateway.oauth import GatewayOAuthProvider
from mindroom.mcp_gateway.selection import GatewaySelections, SelectionAccessDeniedError
from mindroom.mcp_gateway.store import GatewayOAuthCapacityError
from mindroom.mcp_gateway.types import GatewayOwner
from tests.test_mcp_gateway_oauth import provider as provider  # noqa: PLC0414
from tests.test_mcp_gateway_oauth import runtime_paths as runtime_paths  # noqa: PLC0414
from tests.test_mcp_gateway_oauth_capacity import _seed_accounting_v1

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.asyncio
_OWNER = GatewayOwner("@alice:example.org", "@alice:example.org")


@pytest.mark.parametrize("legacy_agents", [[], ["personal", "shared"]])
async def test_legacy_agent_selections_migrate_once_with_exact_accounting(
    provider: GatewayOAuthProvider,
    runtime_paths: RuntimePaths,
    legacy_agents: list[str],
) -> None:
    """Existing all-tools and empty choices survive upgrade without changing byte charges twice."""
    await GatewaySelections(provider.store).set(_OWNER, {})
    await provider.store.transact(
        lambda connection: connection.execute("UPDATE gateway_selections SET agents = ?", (json.dumps(legacy_agents),)),
    )

    def charges(connection: sqlite3.Connection) -> tuple[int, int, int]:
        return (
            connection.execute("SELECT bytes_used FROM oauth_usage").fetchone()[0],
            connection.execute("SELECT bytes_used FROM requester_usage").fetchone()[0],
            connection.execute("SELECT accounted_bytes FROM gateway_selections").fetchone()[0],
        )

    before = await provider.store.read(charges)
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert await GatewaySelections(reopened.store).get(_OWNER, ("different",)) == dict.fromkeys(legacy_agents)
    after = await reopened.store.read(charges)
    assert after[0] - before[0] == after[1] - before[1] == after[2] - before[2]
    again = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert await GatewaySelections(again.store).get(_OWNER, ("different",)) == dict.fromkeys(legacy_agents)
    assert await again.store.read(charges) == after


async def test_tool_choices_are_scoped_to_agent_and_persist(provider: GatewayOAuthProvider) -> None:
    """The same toolkit can be enabled for one agent and withheld from another."""
    selections = GatewaySelections(provider.store)
    choice = {"personal": ("calculator",), "shared": ("duckduckgo",)}
    assert await selections.set(_OWNER, choice) == choice
    other_client = GatewaySelections(provider.store)
    assert await other_client.get(_OWNER, ()) == choice
    other_client.require_selected(_OWNER, "personal", "calculator")
    with pytest.raises(SelectionAccessDeniedError):
        other_client.require_selected(_OWNER, "shared", "calculator")
    await selections.set(_OWNER, {"personal": None})
    other_client.require_selected(_OWNER, "personal", "duckduckgo")


async def test_tool_withdrawal_remains_possible_over_quota(provider: GatewayOAuthProvider) -> None:
    """Withdrawing one toolkit preserves other exposure even when storage is over quota."""
    selections = GatewaySelections(provider.store)
    await selections.set(_OWNER, {"personal": None})
    provider.store.user_max_bytes = 1
    assert await selections.set(_OWNER, {"personal": ("calculator",)}) == {"personal": ("calculator",)}
    with pytest.raises(GatewayOAuthCapacityError):
        await selections.set(_OWNER, {"personal": None})
    with pytest.raises(SelectionAccessDeniedError):
        selections.require_selected(_OWNER, "personal", "duckduckgo")


async def test_default_is_saved_once_and_empty_survives_restart(
    provider: GatewayOAuthProvider,
    runtime_paths: RuntimePaths,
) -> None:
    """A changed personal default or another client cannot overwrite an existing choice."""
    selection = GatewaySelections(provider.store)
    assert await selection.get(_OWNER, ("personal",)) == {"personal": None}
    assert await selection.get(_OWNER, ("replacement",)) == {"personal": None}
    assert await selection.set(_OWNER, {"personal": None, "shared": None}) == {"personal": None, "shared": None}
    second_client = GatewaySelections(provider.store)
    assert await second_client.get(_OWNER, ()) == {"personal": None, "shared": None}
    assert await second_client.set(_OWNER, {}) == {}
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert await GatewaySelections(reopened.store).get(_OWNER, ("personal",)) == {}


@pytest.mark.parametrize("field", ["authenticated_user_id", "requester_id"])
async def test_identity_changes_do_not_inherit_selection(provider: GatewayOAuthProvider, field: str) -> None:
    """Neither an alias nor a canonical identity alone owns another user's settings."""
    selection = GatewaySelections(provider.store)
    await selection.set(_OWNER, {"shared": None})
    other = replace(_OWNER, **{field: "@other:example.org"})
    assert await selection.get(other, ()) == {}
    assert await selection.get(_OWNER, ()) == {"shared": None}


async def test_dispatch_check_reads_committed_selection(provider: GatewayOAuthProvider) -> None:
    """A call prepared before deselection loses authority before the provider starts."""
    selection = GatewaySelections(provider.store)
    await selection.set(_OWNER, {"shared": None})
    selection.require_selected(_OWNER, "shared")
    await GatewaySelections(provider.store).set(_OWNER, {})
    with pytest.raises(SelectionAccessDeniedError):
        selection.require_selected(_OWNER, "shared")


async def test_account_disable_and_recreation_cannot_restore_selection(provider: GatewayOAuthProvider) -> None:
    """Account identity and current activation are required even after tool preparation."""
    accounts = GatewayAccounts(provider.store)
    account = await accounts.create({"userName": "alice@example.org", "active": True})
    owner = replace(_OWNER, account_id=account["id"])
    selection = GatewaySelections(provider.store)
    await selection.set(owner, {"shared": None})
    await provider.store.transact(
        lambda connection: connection.execute(
            "UPDATE gateway_accounts SET active = 0 WHERE account_id = ?",
            (owner.account_id,),
        ),
    )
    with pytest.raises(SelectionAccessDeniedError):
        selection.require_selected(owner, "shared")
    await accounts.delete(account["id"])
    recreated = await accounts.create({"userName": "alice@example.org", "active": True})
    assert await selection.get(replace(owner, account_id=recreated["id"]), ()) == {}
    with pytest.raises(SelectionAccessDeniedError):
        await selection.set(owner, {"shared": None})


async def test_capacity_failure_rolls_back_selection_and_accounting(provider: GatewayOAuthProvider) -> None:
    """Selection writes participate in the same per-user and total storage budgets."""
    selection = GatewaySelections(provider.store)
    await selection.set(_OWNER, {"personal": None})
    before = await provider.store.read(
        lambda connection: tuple(connection.execute("SELECT * FROM oauth_usage").fetchone()),
    )
    provider.store.user_max_bytes = 1
    with pytest.raises(GatewayOAuthCapacityError):
        await selection.set(_OWNER, {"shared": None})
    assert await selection.get(_OWNER, ()) == {"personal": None}
    after = await provider.store.read(
        lambda connection: tuple(connection.execute("SELECT * FROM oauth_usage").fetchone()),
    )
    assert after == before


@pytest.mark.parametrize("global_limit", [False, True], ids=["requester", "global"])
async def test_over_quota_selection_can_only_be_narrowed(
    provider: GatewayOAuthProvider,
    runtime_paths: RuntimePaths,
    global_limit: bool,
) -> None:
    """Lowered storage budgets cannot prevent users from withdrawing existing exposure."""
    selection = GatewaySelections(provider.store)
    await selection.set(_OWNER, {"personal": None, "shared": None})
    if global_limit:
        provider.store.max_bytes = 1
    else:
        provider.store.user_max_bytes = 1
    assert await selection.set(_OWNER, {"personal": None}) == {"personal": None}
    with pytest.raises(SelectionAccessDeniedError):
        selection.require_selected(_OWNER, "shared")
    with pytest.raises(GatewayOAuthCapacityError):
        await selection.set(_OWNER, {"personal": None, "shared": None})
    assert await selection.set(_OWNER, {}) == {}
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert await GatewaySelections(reopened.store).get(_OWNER, ("personal",)) == {}
    with pytest.raises(SelectionAccessDeniedError):
        selection.require_selected(_OWNER, "personal")


async def test_selection_rejects_duplicate_or_unbounded_names(provider: GatewayOAuthProvider) -> None:
    """Storage accepts only bounded unique agent names, including an empty selection."""
    selection = GatewaySelections(provider.store)
    for choices in [
        {"": None},
        {"a" * 257: None},
        {str(i): None for i in range(1001)},
        {"shared": ("calculator", "calculator")},
        {"shared": ("",)},
        {"shared": ("a" * 129,)},
    ]:
        with pytest.raises(ValueError, match="Agent selection"):
            await selection.set(_OWNER, choices)


async def test_agent_bound_authority_is_retired_once(runtime_paths: RuntimePaths) -> None:
    """Old consent cannot silently broaden; accounts and newly issued user grants survive reopen."""
    path = _seed_accounting_v1(runtime_paths)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE gateway_accounts (
            account_id TEXT PRIMARY KEY, user_name TEXT UNIQUE NOT NULL, active INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, profile TEXT NOT NULL, token_valid_after REAL NOT NULL
        )""")
        connection.execute("INSERT INTO gateway_accounts VALUES ('account', 'alice@example.org', 1, 1, 1, '{}', 1)")
    provider = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
        assert connection.execute("SELECT account_id FROM gateway_accounts").fetchone()[0] == "account"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    await GatewaySelections(provider.store).set(_OWNER, {"shared": None})
    reopened = GatewayOAuthProvider(runtime_paths, public_url="https://example.org")
    assert await GatewaySelections(reopened.store).get(_OWNER, ()) == {"shared": None}
