-- Record the billing cycle and amount of the Stripe price each subscription pays, so the Billing page and the
-- admin MRR estimate show what Stripe bills instead of today's monthly plan price.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Apply it before deploying the backend release that writes these columns, because that backend fails to store
-- Stripe subscription events without them. Existing rows fill in on the subscription's next Stripe webhook, or on
-- the next nightly cleanup run for a subscription with an instance.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS billing_cycle TEXT CHECK (billing_cycle IN ('monthly', 'yearly')),
    ADD COLUMN IF NOT EXISTS price_cents INTEGER;

COMMIT;
