-- Instance lifecycle: stop, restart, and tear down hosted instances with their subscriptions.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

-- Store Stripe's subscription statuses; the backend maps Stripe's "canceled" to "cancelled".
-- Only past_due keeps an instance running besides active and unexpired trials.
-- Drop every existing status check, whatever an older schema named it.
DO $$
DECLARE
    status_check RECORD;
BEGIN
    FOR status_check IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'subscriptions'::regclass
            AND contype = 'c'
            AND pg_get_constraintdef(oid) LIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE subscriptions DROP CONSTRAINT %I', status_check.conname);
    END LOOP;
END$$;
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_status_check
    CHECK (status IN ('trialing', 'active', 'cancelled', 'past_due', 'paused', 'incomplete', 'incomplete_expired', 'unpaid'));

-- Set only by the subscription lifecycle: when it stopped the instance, when the instance is torn
-- down unless the subscription becomes entitled again, and the last failed lifecycle step.
ALTER TABLE instances
    ADD COLUMN IF NOT EXISTS lifecycle_stopped_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS teardown_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lifecycle_error TEXT,
    ADD COLUMN IF NOT EXISTS lifecycle_error_at TIMESTAMPTZ;

-- One row per nightly cleanup job run, shown in the admin portal.
CREATE TABLE IF NOT EXISTS cleanup_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    ok BOOLEAN NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Only the backend service role reads or writes cleanup runs.
ALTER TABLE cleanup_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE cleanup_runs FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE cleanup_runs TO service_role;

COMMENT ON TABLE cleanup_runs IS
'Nightly cleanup job results (retention tasks and instance lifecycle). Service role only.';

COMMIT;
