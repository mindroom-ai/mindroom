-- Add auth_token field to instances table for simple authentication
ALTER TABLE instances
ADD COLUMN auth_token TEXT;

-- Add index for quick lookups
CREATE INDEX idx_instances_auth_token ON instances(auth_token);

-- Comment on the new column
COMMENT ON COLUMN instances.auth_token IS 'Simple authentication token for accessing the instance (temporary until proper auth is implemented)';
