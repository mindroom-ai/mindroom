-- Add admin role to accounts table
ALTER TABLE accounts ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;

-- Create index for admin lookups
CREATE INDEX idx_accounts_is_admin ON accounts(is_admin) WHERE is_admin = TRUE;

-- Make basnijholt@gmail.com an admin
UPDATE accounts SET is_admin = TRUE WHERE email = 'basnijholt@gmail.com';

-- Add comment for documentation
COMMENT ON COLUMN accounts.is_admin IS 'Whether this account has admin privileges for the platform';
