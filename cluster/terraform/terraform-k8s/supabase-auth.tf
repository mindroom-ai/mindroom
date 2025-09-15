# ===========================================
# Supabase Authentication Configuration
# ===========================================
# Note: This outputs instructions since Supabase doesn't have a Terraform provider
# OAuth must be configured manually via the Supabase dashboard

locals {
  oauth_configured = var.google_oauth_client_id != "" || var.github_oauth_client_id != ""
}

# Output auth configuration instructions
output "supabase_auth_instructions" {
  sensitive = true
  value = local.oauth_configured ? format("%s\n%s\n%s\n%s",
    "\n========================================\n📋 SUPABASE AUTH CONFIGURATION REQUIRED\n========================================\n\nOAuth credentials are configured in Terraform, but you need to manually set them in Supabase:\n\n1. Go to: https://supabase.com/dashboard/project/lxcziijbiqaxoavavrco\n\n2. Navigate to: Authentication → Providers\n\n3. Configure providers:",
    var.google_oauth_client_id != "" ? "   ✅ Google OAuth - Enable and add credentials" : "   ⚪ Google OAuth - No credentials provided",
    var.github_oauth_client_id != "" ? "   ✅ GitHub OAuth - Enable and add credentials" : "   ⚪ GitHub OAuth - No credentials provided",
    "\n4. Add Redirect URLs (Authentication → URL Configuration):\n   - https://app.${local.dns_domain}/auth/callback\n   - https://app.mindroom.chat/auth/callback\n   - http://localhost:3000/auth/callback\n\n5. Set Site URL:\n   - https://app.${local.dns_domain}\n\n${var.google_oauth_client_id != "" ? "Google Client ID: ${substr(var.google_oauth_client_id, 0, 20)}..." : ""}\n${var.github_oauth_client_id != "" ? "GitHub Client ID: ${substr(var.github_oauth_client_id, 0, 20)}..." : ""}\n\n========================================"
  ) : "No OAuth credentials configured in terraform.tfvars"
}

# Create a marker file to track if auth has been configured
resource "local_file" "auth_config_marker" {
  count = local.oauth_configured ? 1 : 0

  filename = "${path.module}/.auth_configured"
  content = jsonencode({
    google_configured = var.google_oauth_client_id != ""
    github_configured = var.github_oauth_client_id != ""
    timestamp         = timestamp()
    instructions      = "OAuth providers must be manually configured in Supabase dashboard"
    dashboard_url     = "https://supabase.com/dashboard/project/${var.supabase_url != "" ? replace(replace(var.supabase_url, "https://", ""), ".supabase.co", "") : "YOUR_PROJECT"}"
  })
}
