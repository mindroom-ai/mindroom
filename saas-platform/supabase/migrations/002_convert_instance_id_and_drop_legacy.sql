-- Convert instances.instance_id to INTEGER and drop legacy columns

BEGIN;

-- Convert instance_id from TEXT to INTEGER, preserving values
ALTER TABLE instances
    ALTER COLUMN instance_id TYPE INTEGER USING instance_id::integer,
    ALTER COLUMN instance_id SET NOT NULL,
    ALTER COLUMN instance_id SET DEFAULT nextval('instance_id_seq');

-- Ensure subdomain trigger casts integer to text
CREATE OR REPLACE FUNCTION set_subdomain_from_instance_id()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.subdomain IS NULL OR NEW.subdomain = '' THEN
        NEW.subdomain := NEW.instance_id::text;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop legacy column if it exists
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'instances' AND column_name = 'dokku_app_name'
    ) THEN
        ALTER TABLE instances DROP COLUMN dokku_app_name;
    END IF;
END$$;

COMMIT;
