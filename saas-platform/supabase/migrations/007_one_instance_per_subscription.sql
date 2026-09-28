-- Each subscription owns at most one instance, enforced by the database so concurrent provision requests on
-- different backend replicas cannot both insert one.
-- Fresh installs get the same schema from 000_consolidated_complete_schema.sql.
--
-- Safe to paste into the Supabase SQL editor: it runs in one transaction and every
-- statement is idempotent, so a partial or repeated run leaves the same end state.
-- It fails without changing anything while a subscription still has several instance rows.
-- Find them first:
--
--   SELECT subscription_id, array_agg(instance_id ORDER BY instance_id), array_agg(status ORDER BY instance_id)
--   FROM instances GROUP BY subscription_id HAVING count(*) > 1;
--
-- and resolve each one as docs/deployment/kubernetes.md#database-migrations describes.

BEGIN;

DO $$
DECLARE
    duplicates TEXT;
BEGIN
    SELECT string_agg(format('subscription %s has instances %s', subscription_id, instance_ids), '; ')
    INTO duplicates
    FROM (
        SELECT subscription_id, array_agg(instance_id ORDER BY instance_id) AS instance_ids
        FROM instances
        GROUP BY subscription_id
        HAVING count(*) > 1
    ) AS duplicated;
    IF duplicates IS NOT NULL THEN
        RAISE EXCEPTION 'Resolve subscriptions with several instance rows first: %', duplicates;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'instances'::regclass AND conname = 'instances_subscription_id_key'
    ) THEN
        ALTER TABLE instances ADD CONSTRAINT instances_subscription_id_key UNIQUE (subscription_id);
    END IF;
END$$;

-- The unique constraint's index replaces the plain lookup index.
DROP INDEX IF EXISTS idx_instances_subscription_id;

COMMIT;
