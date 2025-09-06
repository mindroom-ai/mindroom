# ===========================================
# DNS Configuration (Porkbun)
# ===========================================

provider "porkbun" {
  api_key    = var.porkbun_api_key
  secret_key = var.porkbun_secret_key
}

# Get the subdomain based on environment
locals {
  # For staging: staging.mindroom.chat
  # For production: mindroom.chat
  dns_domain = var.environment == "production" ? var.domain : "${var.environment}.${var.domain}"

  # Platform subdomains
  platform_subdomains = ["app", "admin", "api", "webhooks"]
}

# A records for platform services
resource "porkbun_dns_record" "platform_a" {
  for_each = toset(local.platform_subdomains)

  domain  = var.domain
  name    = var.environment == "production" ? each.value : "${each.value}.${var.environment}"
  type    = "A"
  content = module.kube-hetzner.control_plane_nodes[0].ipv4_address
  ttl     = "600"
}

# AAAA records for platform services (IPv6)
resource "porkbun_dns_record" "platform_aaaa" {
  for_each = toset(local.platform_subdomains)

  domain  = var.domain
  name    = var.environment == "production" ? each.value : "${each.value}.${var.environment}"
  type    = "AAAA"
  content = module.kube-hetzner.control_plane_nodes[0].ipv6_address
  ttl     = "600"
}

# Wildcard A record for customer instances
resource "porkbun_dns_record" "wildcard_a" {
  domain  = var.domain
  name    = var.environment == "production" ? "*" : "*.${var.environment}"
  type    = "A"
  content = module.kube-hetzner.control_plane_nodes[0].ipv4_address
  ttl     = "600"
}

# Wildcard AAAA record for customer instances (IPv6)
resource "porkbun_dns_record" "wildcard_aaaa" {
  domain  = var.domain
  name    = var.environment == "production" ? "*" : "*.${var.environment}"
  type    = "AAAA"
  content = module.kube-hetzner.control_plane_nodes[0].ipv6_address
  ttl     = "600"
}

# ===========================================
# DNS Output
# ===========================================

output "dns_records" {
  value = {
    platform = [for subdomain in local.platform_subdomains :
      "${subdomain}.${local.dns_domain} → ${module.kube-hetzner.control_plane_nodes[0].ipv4_address}"
    ]
    wildcard = "*.${local.dns_domain} → ${module.kube-hetzner.control_plane_nodes[0].ipv4_address}"
  }
  description = "DNS records configured"
}
