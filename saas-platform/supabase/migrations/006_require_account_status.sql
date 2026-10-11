-- Every account has one of the statuses the backend knows; only 'active' accounts may use the platform API.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.
-- It fails without changing anything if an account holds a status outside the list below;
-- correct those rows first.

BEGIN;

-- The column always defaulted to 'active', so a NULL status was an active account.
UPDATE accounts SET status = 'active' WHERE status IS NULL;

ALTER TABLE accounts ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE accounts ALTER COLUMN status SET NOT NULL;

ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_status_check;
ALTER TABLE accounts
    ADD CONSTRAINT accounts_status_check
    CHECK (status IN ('active', 'suspended', 'deleted', 'pending_verification'));

COMMIT;
