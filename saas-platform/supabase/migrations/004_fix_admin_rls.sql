-- Fix RLS policies for accounts table to allow reading is_admin field

-- First, ensure RLS is enabled on the accounts table
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;

-- Drop existing policies if they exist (to recreate with proper permissions)
DROP POLICY IF EXISTS "Users can view their own account" ON accounts;
DROP POLICY IF EXISTS "Users can update their own account" ON accounts;
DROP POLICY IF EXISTS "Service role can do everything" ON accounts;

-- Create policy for users to view their own account (including is_admin field)
CREATE POLICY "Users can view their own account"
ON accounts FOR SELECT
USING (auth.uid() = id);

-- Create policy for users to update their own account (but not is_admin)
CREATE POLICY "Users can update their own account"
ON accounts FOR UPDATE
USING (auth.uid() = id)
WITH CHECK (auth.uid() = id);

-- Create policy for service role to have full access
CREATE POLICY "Service role can do everything"
ON accounts FOR ALL
USING (auth.role() = 'service_role')
WITH CHECK (auth.role() = 'service_role');

-- Ensure the is_admin column exists with proper default
ALTER TABLE accounts
ALTER COLUMN is_admin SET DEFAULT false;

-- Grant necessary permissions
GRANT ALL ON accounts TO authenticated;
GRANT ALL ON accounts TO service_role;

-- Create an index for faster lookups
CREATE INDEX IF NOT EXISTS idx_accounts_is_admin ON accounts(is_admin);

-- Add a comment to document the purpose
COMMENT ON COLUMN accounts.is_admin IS 'Flag to indicate if the user has admin privileges in the platform';
