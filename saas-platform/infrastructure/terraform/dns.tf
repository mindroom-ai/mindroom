# ===========================================
# Porkbun DNS Configuration
# ===========================================

# Configure Porkbun Provider
provider "porkbun" {
  api_key    = var.porkbun_api_key
  secret_key = var.porkbun_secret_key
}

# ===========================================
# DNS Records for MindRoom Platform
# ===========================================
#
# IMPORTANT: Terraform will only manage the records defined below.
# Your existing DNS records will NOT be deleted or modified unless
# they have the exact same name/type combination as defined here.
#
# If you have existing records that conflict (e.g., existing A record
# for your domain), you can:
# 1. Import them: terraform import porkbun_dns_record.main_a <record_id>
# 2. Or delete them manually in Porkbun before running terraform apply
# ===========================================

# Main domain A record
resource "porkbun_dns_record" "main_a" {
  domain  = var.domain
  name    = ""
  type    = "A"
  content = hcloud_server.platform.ipv4_address
  ttl     = "600"
}

# Main domain AAAA record (IPv6)
resource "porkbun_dns_record" "main_aaaa" {
  domain  = var.domain
  name    = ""
  type    = "AAAA"
  content = hcloud_server.platform.ipv6_address
  ttl     = "600"
}

# Platform services subdomains
locals {
  platform_subdomains = ["app", "admin", "api", "webhooks"]
}

# A records for platform services
resource "porkbun_dns_record" "platform_a" {
  for_each = toset(local.platform_subdomains)

  domain  = var.domain
  name    = each.value
  type    = "A"
  content = hcloud_server.platform.ipv4_address
  ttl     = "600"
}

# AAAA records for platform services (IPv6)
resource "porkbun_dns_record" "platform_aaaa" {
  for_each = toset(local.platform_subdomains)

  domain  = var.domain
  name    = each.value
  type    = "AAAA"
  content = hcloud_server.platform.ipv6_address
  ttl     = "600"
}

# Wildcard A record for customer instances
resource "porkbun_dns_record" "wildcard_a" {
  domain  = var.domain
  name    = "*"
  type    = "A"
  content = hcloud_server.dokku.ipv4_address
  ttl     = "600"
}

# Wildcard AAAA record for customer instances (IPv6)
resource "porkbun_dns_record" "wildcard_aaaa" {
  domain  = var.domain
  name    = "*"
  type    = "AAAA"
  content = hcloud_server.dokku.ipv6_address
  ttl     = "600"
}

# Matrix federation wildcard (optional)
resource "porkbun_dns_record" "matrix_wildcard_a" {
  count = var.enable_matrix_federation ? 1 : 0

  domain  = var.domain
  name    = "*.m"
  type    = "A"
  content = hcloud_server.dokku.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "matrix_wildcard_aaaa" {
  count = var.enable_matrix_federation ? 1 : 0

  domain  = var.domain
  name    = "*.m"
  type    = "AAAA"
  content = hcloud_server.dokku.ipv6_address
  ttl     = "600"
}

# Optional: MX records for email
resource "porkbun_dns_record" "mx" {
  count = var.enable_email ? 1 : 0

  domain   = var.domain
  name     = ""
  type     = "MX"
  content  = "10 ${var.mx_server}"
  ttl      = "3600"
}

# Optional: SPF record for email
resource "porkbun_dns_record" "spf" {
  count = var.enable_email ? 1 : 0

  domain  = var.domain
  name    = ""
  type    = "TXT"
  content = "v=spf1 mx ~all"
  ttl     = "3600"
}

# ===========================================
# Outputs for DNS Status
# ===========================================

output "dns_records_created" {
  value = {
    status          = "✅ DNS records automatically configured via Porkbun"
    main_domain     = "${var.domain} → ${hcloud_server.platform.ipv4_address}"
    wildcard        = "*.${var.domain} → ${hcloud_server.dokku.ipv4_address}"
    platform_apps   = [for subdomain in local.platform_subdomains : "${subdomain}.${var.domain} → ${hcloud_server.platform.ipv4_address}"]
    ipv6_configured = true
    note            = "Only records defined in dns.tf are managed. Other DNS records remain untouched."
  }
  description = "DNS records managed by Terraform"
}
