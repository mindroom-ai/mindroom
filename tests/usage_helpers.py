"""Check what a session's runs counted: its usage ledger, the usage report's totals, and monthly spend."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.budgets.pricing import PricedModel
from mindroom.budgets.spend import collect_monthly_spend
from mindroom.config.models import ModelPricing
from mindroom.session_storage.schema import quote_identifier
from mindroom.usage.stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelIdentity
    from mindroom.session_storage.store import ConversationStore

# The prices the spend check charges per million input and output tokens.
_INPUT_PRICE, _OUTPUT_PRICE = 2, 10


def counted_usage(storage: ConversationStore, session_id: str) -> list[tuple[int, int]]:
    """Return the input and output tokens of each usage snapshot the session's ledger counts.

    A snapshot omits a zero counter, which reads as 0.
    """
    with storage.transaction(write=False) as connection:
        snapshots = connection.execute(
            f"SELECT usage_data FROM {quote_identifier(storage.names.usage)} WHERE session_id = ?",  # noqa: S608
            (session_id,),
        ).fetchall()
    return [
        (metrics.get("input_tokens", 0), metrics.get("output_tokens", 0))
        for metrics in (json.loads(snapshot)["metrics"] for (snapshot,) in snapshots)
    ]


def counted_total(storage: ConversationStore, session_id: str) -> tuple[int, int]:
    """Return the input and output tokens the session's ledger counts over all its usage snapshots."""
    counted = counted_usage(storage, session_id)
    return sum(tokens[0] for tokens in counted), sum(tokens[1] for tokens in counted)


def assert_counted_usage(
    storage: ConversationStore,
    session_id: str,
    expected: Sequence[tuple[int, int]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    model: ModelIdentity,
    requester_id: str,
) -> None:
    """Assert the session's usage snapshots are the ``expected`` input and output tokens, in any order.

    Their sum must be the usage report's session totals and ``requester_id``'s monthly spend on ``model``.
    """
    assert sorted(counted_usage(storage, session_id)) == sorted(expected)
    totals = (sum(tokens[0] for tokens in expected), sum(tokens[1] for tokens in expected))
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths)
    assert (report.totals.input_tokens, report.totals.output_tokens) == totals
    priced = PricedModel(pricing=ModelPricing(input=_INPUT_PRICE, output=_OUTPUT_PRICE), input_includes_cache=False)
    spend = collect_monthly_spend(config, runtime_paths, datetime.now(UTC), {(model.provider_name, model.id): priced})
    assert spend.spend_usd == {
        requester_id: pytest.approx((totals[0] * _INPUT_PRICE + totals[1] * _OUTPUT_PRICE) / 1_000_000),
    }


def usage_rows(storage: ConversationStore, run_id: str) -> list[dict[str, Any]]:
    """Return the usage ledger rows of one run."""
    with storage.transaction(write=False) as connection:
        rows = connection.execute(
            f"SELECT usage_data FROM {quote_identifier(storage.names.usage)} WHERE run_id = ?",  # noqa: S608
            (run_id,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


__all__ = ["assert_counted_usage", "counted_total", "counted_usage", "usage_rows"]
