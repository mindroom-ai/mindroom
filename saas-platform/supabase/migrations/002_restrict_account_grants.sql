-- Apply account grant hardening to databases that already ran the baseline schema.
-- Fresh installs get the same grants from 000_consolidated_complete_schema.sql.

REVOKE INSERT, UPDATE ON TABLE accounts FROM authenticated;
GRANT SELECT ON TABLE accounts TO authenticated;
GRANT UPDATE (full_name, company_name, consent_marketing, consent_analytics, consent_updated_at) ON TABLE accounts TO authenticated;
