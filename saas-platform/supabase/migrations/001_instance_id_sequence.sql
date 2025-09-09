-- Instance ID sequence and defaulting for instances
-- This migration introduces a sequence to generate numeric string instance IDs

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = 'instance_id_seq'
    ) THEN
        CREATE SEQUENCE instance_id_seq START 1;
    END IF;
END$$;

-- Initialize sequence to max(existing)+1 to avoid collisions
SELECT setval(
    'instance_id_seq',
    COALESCE((SELECT MAX((instance_id)::int) FROM instances), 0)
);

-- Set default for instance_id to next sequence value cast to text
ALTER TABLE instances
    ALTER COLUMN instance_id SET DEFAULT nextval('instance_id_seq')::text;

-- Ensure subdomain defaults to instance_id if not provided
CREATE OR REPLACE FUNCTION set_subdomain_from_instance_id()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.subdomain IS NULL OR NEW.subdomain = '' THEN
        NEW.subdomain := NEW.instance_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_subdomain_from_instance_id ON instances;
CREATE TRIGGER trg_set_subdomain_from_instance_id
BEFORE INSERT ON instances
FOR EACH ROW EXECUTE PROCEDURE set_subdomain_from_instance_id();
