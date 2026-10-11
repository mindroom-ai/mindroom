-- Drop the per-subscription agent and message limits, which nothing enforced or displayed.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Apply it after deploying the backend release that stops writing these columns, because older backends
-- fail to create or update subscriptions without them.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

ALTER TABLE subscriptions
    DROP COLUMN IF EXISTS max_agents,
    DROP COLUMN IF EXISTS max_messages_per_day;

COMMIT;
