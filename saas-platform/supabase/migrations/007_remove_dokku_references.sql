-- Remove all Dokku references and use K8s-appropriate naming

-- Rename dokku_app_name to instance_id (unique identifier for the instance)
ALTER TABLE instances
RENAME COLUMN dokku_app_name TO instance_id;

-- Update the comment
COMMENT ON COLUMN instances.instance_id IS 'Unique identifier for the Kubernetes instance (e.g., sub1757)';

-- The subdomain column stays the same as it's still used for the URL
