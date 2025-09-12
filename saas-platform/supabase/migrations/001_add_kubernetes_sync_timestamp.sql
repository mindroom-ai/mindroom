-- Add timestamp to track when Kubernetes status was last synced
ALTER TABLE instances
ADD COLUMN IF NOT EXISTS kubernetes_synced_at TIMESTAMPTZ;

-- Set initial value to created_at for existing instances
UPDATE instances
SET kubernetes_synced_at = created_at
WHERE kubernetes_synced_at IS NULL;

-- Create index for efficient querying of stale instances
CREATE INDEX IF NOT EXISTS idx_instances_kubernetes_synced_at
ON instances(kubernetes_synced_at);
