-- Drop the usage_metrics table: no collector ever filled it, and nothing reads it any more.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Apply it after deploying the backend release that stops reading the table, because older backends
-- query it for the GDPR export, admin metrics, and nightly cleanup.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.

BEGIN;

DROP TABLE IF EXISTS usage_metrics;

COMMIT;
