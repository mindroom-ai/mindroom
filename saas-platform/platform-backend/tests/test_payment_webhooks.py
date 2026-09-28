"""Stripe invoice webhooks, in the payload shape of the pinned Stripe API version, write only real table columns."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import stripe
from backend.routes.gdpr import export_user_data
from fastapi.testclient import TestClient
from main import app

from tests.fake_supabase import FakeQuery, FakeResult, FakeSupabase

ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION_ROW_ID = "11111111-1111-1111-1111-111111111111"
WEBHOOK_SECRET = "whsec_payment_test"  # noqa: S105
SCHEMA = Path(__file__).resolve().parents[2] / "supabase/migrations/000_consolidated_complete_schema.sql"


def _schema_columns() -> dict[str, set[str]]:
    """Column names of every table in the consolidated schema, which the incremental migrations keep in sync."""
    sql = SCHEMA.read_text(encoding="utf-8")
    tables = re.findall(r"^CREATE TABLE (\w+) \((.*?)^\);", sql, re.MULTILINE | re.DOTALL)
    return {name: set(re.findall(r"^\s+([a-z_]+)\s+[A-Z]", body, re.MULTILINE)) for name, body in tables}


TABLE_COLUMNS = _schema_columns()


class _SchemaCheckedQuery(FakeQuery):
    """Reject writes, filters, and selects that name a column the real table does not have."""

    def execute(self) -> FakeResult:
        assert self.table_name in TABLE_COLUMNS, f"table {self.table_name} does not exist"
        used = set(self.payload or {}) | {column for _op, column, _value in self.filters}
        used |= {column.strip() for column in self.columns.split(",")} - {"*"}
        unknown = used - TABLE_COLUMNS[self.table_name]
        assert not unknown, f"{self.table_name} has no columns {sorted(unknown)}"
        return super().execute()


class _SchemaCheckedSupabase(FakeSupabase):
    def table(self, name: str) -> FakeQuery:
        return _SchemaCheckedQuery(self, name)


def _invoice(invoice_id: str = "in_1", *, status: str = "paid", amount_paid: int = 2900) -> dict[str, Any]:
    """A subscription invoice as Stripe sends it since API version 2025-03-31.basil (the lib pins 2025-08-27.basil).

    The subscription moved from ``invoice.subscription`` to ``invoice.parent.subscription_details.subscription``.
    """
    return {
        "id": invoice_id,
        "object": "invoice",
        "account_country": "US",
        "amount_due": 2900,
        "amount_paid": amount_paid,
        "amount_remaining": 2900 - amount_paid,
        "attempt_count": 1,
        "attempted": True,
        "billing_reason": "subscription_cycle",
        "collection_method": "charge_automatically",
        "created": 1_787_000_000,
        "currency": "usd",
        "customer": "cus_1",
        "customer_email": "customer@example.com",
        "lines": {
            "object": "list",
            "data": [
                {
                    "id": "il_1",
                    "object": "line_item",
                    "amount": 2900,
                    "currency": "usd",
                    "description": "1 x MindRoom Hobby (at $29.00 / month)",
                    "parent": {
                        "type": "subscription_item_details",
                        "subscription_item_details": {
                            "subscription": "sub_stripe_1",
                            "subscription_item": "si_1",
                            "invoice_item": None,
                            "proration": False,
                            "proration_details": {"credited_items": None},
                        },
                        "invoice_item_details": None,
                    },
                    "period": {"start": 1_787_000_000, "end": 1_789_678_400},
                    "pricing": {
                        "type": "price_details",
                        "price_details": {"price": "price_hobby", "product": "prod_hobby"},
                        "unit_amount_decimal": "2900",
                    },
                    "quantity": 1,
                }
            ],
            "has_more": False,
            "total_count": 1,
            "url": f"/v1/invoices/{invoice_id}/lines",
        },
        "livemode": True,
        "number": "MR-0001",
        "parent": {
            "type": "subscription_details",
            "quote_details": None,
            "subscription_details": {"metadata": {}, "subscription": "sub_stripe_1"},
        },
        "period_end": 1_787_000_000,
        "period_start": 1_784_321_600,
        "status": status,
        "subtotal": 2900,
        "total": 2900,
    }


@pytest.fixture
def db() -> Iterator[_SchemaCheckedSupabase]:
    db = _SchemaCheckedSupabase(
        {
            "accounts": [{"id": ACCOUNT_ID, "email": "customer@example.com", "stripe_customer_id": "cus_1"}],
            "subscriptions": [
                {
                    "id": SUBSCRIPTION_ROW_ID,
                    "account_id": ACCOUNT_ID,
                    "stripe_subscription_id": "sub_stripe_1",
                    "tier": "hobby",
                    "status": "active",
                }
            ],
            "payments": [],
            "webhook_events": [],
        }
    )
    with (
        patch("backend.routes.webhooks.ensure_supabase", return_value=db),
        patch("backend.routes.webhooks.STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET),
        patch("backend.routes.webhooks.reconcile_account_instances", new=AsyncMock()),
    ):
        yield db


def _deliver(event_type: str, invoice: dict[str, Any], event_id: str = "evt_1") -> dict[str, Any]:
    """Sign and post the event the way Stripe does, so the handler sees a real ``stripe.StripeObject``."""
    body = json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": stripe.api_version,
            "created": 1_787_000_100,
            "data": {"object": invoice},
            "livemode": True,
            "pending_webhooks": 1,
            "request": {"id": None, "idempotency_key": None},
            "type": event_type,
        }
    )
    timestamp = int(time.time())
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    response = TestClient(app).post(
        "/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={timestamp},v1={signature}"}
    )
    assert response.status_code == 200
    return response.json()


def test_stripe_library_pins_a_basil_api_version() -> None:
    """The invoice fixture above follows basil; revisit it when the Stripe library moves to a new major version."""
    assert stripe.api_version.endswith(".basil")


def test_payment_succeeded_records_payment(db: _SchemaCheckedSupabase) -> None:
    assert _deliver("invoice.payment_succeeded", _invoice()) == {"received": True, "error": None}

    payment = db.row("payments", invoice_id="in_1")
    assert payment == {
        "id": payment["id"],
        "invoice_id": "in_1",
        "subscription_id": "sub_stripe_1",
        "customer_id": "cus_1",
        "account_id": ACCOUNT_ID,
        "amount": 29.0,
        "currency": "usd",
        "status": "succeeded",
    }
    event = db.row("webhook_events", stripe_event_id="evt_1")
    assert event["account_id"] == ACCOUNT_ID
    assert "error" not in event


def test_payment_succeeded_falls_back_to_subscription_account(db: _SchemaCheckedSupabase) -> None:
    db.row("accounts", id=ACCOUNT_ID)["stripe_customer_id"] = "cus_other"

    assert _deliver("invoice.payment_succeeded", _invoice()) == {"received": True, "error": None}

    assert db.row("payments", invoice_id="in_1")["account_id"] == ACCOUNT_ID
    assert db.row("webhook_events", stripe_event_id="evt_1")["account_id"] == ACCOUNT_ID


def test_payment_succeeded_without_any_account_writes_no_payment(db: _SchemaCheckedSupabase) -> None:
    db.row("accounts", id=ACCOUNT_ID)["stripe_customer_id"] = "cus_other"
    db.row("subscriptions", id=SUBSCRIPTION_ROW_ID)["stripe_subscription_id"] = "sub_other"

    assert _deliver("invoice.payment_succeeded", _invoice()) == {"received": True, "error": "Failed to process payment"}

    assert db.tables["payments"] == []
    event = db.row("webhook_events", stripe_event_id="evt_1")
    assert "account_id" not in event
    assert event["error"] == "Failed to process payment"


def test_gdpr_export_includes_recorded_payments(db: _SchemaCheckedSupabase) -> None:
    _deliver("invoice.payment_succeeded", _invoice())

    with patch("backend.routes.gdpr.ensure_supabase", return_value=db):
        export = asyncio.run(export_user_data({"account_id": ACCOUNT_ID}))

    assert [payment["invoice_id"] for payment in export["payments"]] == ["in_1"]


def test_redelivered_payment_succeeded_keeps_one_payment(db: _SchemaCheckedSupabase) -> None:
    _deliver("invoice.payment_succeeded", _invoice(), event_id="evt_1")
    assert _deliver("invoice.payment_succeeded", _invoice(), event_id="evt_2") == {"received": True, "error": None}

    assert len(db.tables["payments"]) == 1


def test_payment_failed_marks_active_subscription_past_due(db: _SchemaCheckedSupabase) -> None:
    invoice = _invoice(status="open", amount_paid=0)

    assert _deliver("invoice.payment_failed", invoice) == {"received": True, "error": None}

    assert db.row("subscriptions", id=SUBSCRIPTION_ROW_ID)["status"] == "past_due"
    event = db.row("webhook_events", stripe_event_id="evt_1")
    assert event["account_id"] == ACCOUNT_ID
    assert "error" not in event


def test_invoice_without_subscription_is_not_recorded(db: _SchemaCheckedSupabase) -> None:
    invoice = _invoice() | {"parent": None}

    assert _deliver("invoice.payment_succeeded", invoice) == {"received": True, "error": "Failed to process payment"}
    assert db.tables["payments"] == []
