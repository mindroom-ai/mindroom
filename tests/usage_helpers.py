"""Check what a session's runs counted: its usage ledger, the usage report's totals, and monthly spend."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    names = storage.names
    with storage.transaction(write=False) as connection:
        snapshots = connection.execute(
            f"SELECT usage_data FROM {quote_identifier(names.usage)} WHERE session_id = ?",  # noqa: S608
            (session_id,),
        ).fetchall()
    counted = sorted(
        (metrics["input_tokens"], metrics["output_tokens"])
        for metrics in (json.loads(snapshot)["metrics"] for (snapshot,) in snapshots)
    )
    assert counted == sorted(expected)
    totals = (sum(tokens[0] for tokens in expected), sum(tokens[1] for tokens in expected))
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths)
    assert (report.totals.input_tokens, report.totals.output_tokens) == totals
    priced = PricedModel(pricing=ModelPricing(input=_INPUT_PRICE, output=_OUTPUT_PRICE), input_includes_cache=False)
    spend = collect_monthly_spend(config, runtime_paths, datetime.now(UTC), {(model.provider_name, model.id): priced})
    assert spend.spend_usd == {
        requester_id: pytest.approx((totals[0] * _INPUT_PRICE + totals[1] * _OUTPUT_PRICE) / 1_000_000),
    }


__all__ = ["assert_counted_usage"]
