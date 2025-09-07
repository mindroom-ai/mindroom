-- Add matrix_server_url column to instances table
ALTER TABLE instances ADD COLUMN IF NOT EXISTS matrix_server_url TEXT;

-- Add dokku_app_name column if missing (for legacy compatibility)
ALTER TABLE instances ADD COLUMN IF NOT EXISTS dokku_app_name TEXT;

-- Update the comment for clarity
COMMENT ON COLUMN instances.matrix_server_url IS 'URL of the Matrix (Synapse) server for this instance';
COMMENT ON COLUMN instances.dokku_app_name IS 'Legacy field for Dokku compatibility, now stores K8s app identifier';

-- Update existing instances to have the correct matrix URL based on their instance_id
UPDATE instances
SET matrix_server_url = CONCAT('https://', instance_id, '.matrix.staging.mindroom.chat')
WHERE matrix_server_url IS NULL AND instance_id IS NOT NULL;
