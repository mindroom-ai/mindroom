"""The payments backfill pages through paid Stripe invoices and writes exactly what the webhook would."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import stripe
from backend.scripts.backfill_payments import backfill_payments, main

from tests.test_payment_webhooks import (
    ACCOUNT_ID,
    WEBHOOK_SECRET,
    _deliver,
    _invoice,
    _SchemaCheckedSupabase,
    seeded_db,
)

PAGE_SIZE = 2


class _FakeStripeHTTP(stripe.HTTPClient):
    """Serve ``GET /v1/invoices`` in pages of two, so the real ``auto_paging_iter`` has to follow ``starting_after``."""

    name: ClassVar[str] = "fake"

    def __init__(self, invoices: list[dict[str, Any]]) -> None:
        super().__init__()
        self.invoices = invoices
        self.queries: list[dict[str, list[str]]] = []

    def request(self, method: str, url: str, headers: Any, post_data: Any = None, **_kwargs: Any) -> tuple:  # noqa: ANN401
        parts = urlsplit(url)
        assert (method, parts.path) == ("get", "/v1/invoices")
        query = parse_qs(parts.query)
        self.queries.append(query)
        assert query["status"] == ["paid"]
        ids = [invoice["id"] for invoice in self.invoices]
        start = ids.index(query["starting_after"][0]) + 1 if "starting_after" in query else 0
        page = self.invoices[start : start + PAGE_SIZE]
        body = {
            "object": "list",
            "data": page,
            "has_more": start + PAGE_SIZE < len(self.invoices),
            "url": "/v1/invoices",
        }
        return json.dumps(body), 200, {}

    def close(self) -> None:
        pass


def _other_customer_invoice(invoice_id: str) -> dict[str, Any]:
    """A paid invoice whose customer and subscription no account knows."""
    invoice = _invoice(invoice_id)
    invoice["customer"] = "cus_unknown"
    invoice["parent"]["subscription_details"]["subscription"] = "sub_unknown"
    return invoice


INVOICES = [
    _invoice("in_1"),
    _invoice("in_2", amount_paid=9600),
    _invoice("in_free", amount_paid=0),
    _other_customer_invoice("in_orphan"),
    _invoice("in_one_off") | {"parent": None},
]


@pytest.fixture
def stripe_http() -> Iterator[_FakeStripeHTTP]:
    client = _FakeStripeHTTP(copy.deepcopy(INVOICES))
    with patch.object(stripe, "default_http_client", client), patch.object(stripe, "api_key", "sk_test_fake"):
        yield client


@pytest.fixture
def db() -> Iterator[_SchemaCheckedSupabase]:
    db = seeded_db()
    with (
        patch("backend.scripts.backfill_payments.ensure_supabase", return_value=db),
        patch("backend.routes.webhooks.ensure_supabase", return_value=db),
    ):
        yield db


def test_dry_run_writes_nothing(
    db: _SchemaCheckedSupabase, stripe_http: _FakeStripeHTTP, capsys: pytest.CaptureFixture[str]
) -> None:
    before = copy.deepcopy(db.tables)

    result = backfill_payments(apply=False)

    assert db.tables == before
    assert result.written == ["in_1", "in_2"]
    assert [query.get("starting_after") for query in stripe_http.queries] == [None, ["in_2"], ["in_orphan"]]
    output = capsys.readouterr().out
    assert "WOULD in_1 2026-08-17 29.00 USD customer@example.com" in output
    assert "WOULD in_2 2026-08-17 96.00 USD customer@example.com" in output
    assert "in_free" not in output
    assert "Dry run: nothing was written" in output


def test_dry_run_is_the_default(db: _SchemaCheckedSupabase, stripe_http: _FakeStripeHTTP) -> None:  # noqa: ARG001
    main([])
    main(["--dry-run"])

    assert db.tables["payments"] == []


def test_apply_writes_the_rows_the_webhook_would(db: _SchemaCheckedSupabase, stripe_http: _FakeStripeHTTP) -> None:  # noqa: ARG001
    main(["--apply"])

    webhook_db = seeded_db()
    with (
        patch("backend.routes.webhooks.ensure_supabase", return_value=webhook_db),
        patch("backend.routes.webhooks.STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET),
        patch("backend.routes.webhooks.reconcile_account_instances", new=AsyncMock()),
    ):
        _deliver("invoice.payment_succeeded", _invoice("in_1"), event_id="evt_1")
        _deliver("invoice.payment_succeeded", _invoice("in_2", amount_paid=9600), event_id="evt_2")

    def without_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{key: value for key, value in row.items() if key != "id"} for row in rows]

    assert without_ids(db.tables["payments"]) == without_ids(webhook_db.tables["payments"])
    assert [row["account_id"] for row in db.tables["payments"]] == [ACCOUNT_ID, ACCOUNT_ID]


def test_rerunning_apply_changes_nothing(db: _SchemaCheckedSupabase, stripe_http: _FakeStripeHTTP) -> None:  # noqa: ARG001
    backfill_payments(apply=True)
    after_first = copy.deepcopy(db.tables)

    result = backfill_payments(apply=True)

    assert db.tables == after_first
    assert result.written == ["in_1", "in_2"]


def test_invoices_without_an_account_are_skipped_and_reported(
    db: _SchemaCheckedSupabase,
    stripe_http: _FakeStripeHTTP,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = backfill_payments(apply=True)

    assert result.skipped == ["in_orphan", "in_one_off"]
    assert {row["invoice_id"] for row in db.tables["payments"]} == {"in_1", "in_2"}
    output = capsys.readouterr().out
    assert "SKIP  in_orphan: no subscription or no account (customer cus_unknown, customer@example.com)" in output
    assert "Wrote 2 payments, skipped 2 invoices without an account." in output
