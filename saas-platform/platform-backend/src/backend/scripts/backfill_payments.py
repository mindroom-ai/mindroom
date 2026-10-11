"""Backfill the ``payments`` table from paid Stripe invoices.

Until the ``invoice.payment_succeeded`` webhook read the Stripe basil invoice shape, every paid invoice failed to reach
``payments``. This writes them with the same row-building code the webhook uses. Run inside the platform-backend
container, which already has the Stripe and Supabase credentials:

    python -m backend.scripts.backfill_payments            # dry run: print what would be written
    python -m backend.scripts.backfill_payments --apply    # upsert on invoice_id; safe to re-run
"""

import argparse
from dataclasses import dataclass, field
from typing import Any

from backend.config import stripe
from backend.deps import ensure_supabase
from backend.routes.webhooks import payment_row, upsert_payment


@dataclass
class BackfillResult:
    """Invoice ids that were (or in a dry run would be) written, and those skipped for lack of an account."""

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _account_email(sb: Any, account_id: str) -> str | None:
    rows = sb.table("accounts").select("email").eq("id", account_id).limit(1).execute().data
    return rows[0]["email"] if rows else None


def backfill_payments(*, apply: bool) -> BackfillResult:
    """Walk every paid Stripe invoice and record it in ``payments``; only print the rows unless ``apply`` is set."""
    sb = ensure_supabase()
    result = BackfillResult()
    for invoice in stripe.Invoice.list(status="paid", limit=100).auto_paging_iter():
        if invoice["amount_paid"] <= 0:
            continue
        row = payment_row(sb, invoice)
        if row is None:
            print(
                f"SKIP  {invoice['id']}: no subscription or no account "
                f"(customer {invoice['customer']}, {invoice.get('customer_email')})"
            )
            result.skipped.append(invoice["id"])
            continue
        who = _account_email(sb, row["account_id"]) or row["account_id"]
        print(
            f"{'WRITE' if apply else 'WOULD'} {row['invoice_id']} {row['created_at'][:10]} {row['amount']:.2f} {row['currency'].upper()} {who}"
        )
        if apply:
            upsert_payment(sb, row)
        result.written.append(row["invoice_id"])
    verb = "Wrote" if apply else "Would write"
    print(f"{verb} {len(result.written)} payments, skipped {len(result.skipped)} invoices without an account.")
    if not apply:
        print("Dry run: nothing was written. Re-run with --apply to write.")
    return result


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the backfill."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print what would be written (default)")
    mode.add_argument("--apply", action="store_true", help="upsert the payments on invoice_id")
    args = parser.parse_args(argv)
    backfill_payments(apply=args.apply)


if __name__ == "__main__":
    main()
