# DNS Module - Creates DNS records for the platform
#
# Requires both the superdomain (where platform is deployed) and root domain (for DNS API)
# Examples:
# - Production: superdomain="mindroom.chat", root_domain="mindroom.chat"
# - Staging: superdomain="staging.mindroom.chat", root_domain="mindroom.chat"

terraform {
  required_providers {
    porkbun = {
      source  = "cullenmcdermott/porkbun"
      version = "~> 0.2"
    }
  }
}

variable "superdomain" {
  type        = string
  description = "The full domain where the platform is deployed (e.g., staging.mindroom.chat)"
}
variable "root_domain" {
  type        = string
  description = "The root domain registered with DNS provider (e.g., mindroom.chat)"
}
variable "porkbun_api_key" { type = string }
variable "porkbun_secret_key" { type = string }
variable "ipv4_address" { type = string }
variable "ipv6_address" { type = string }

locals {
  # Calculate the subdomain prefix by removing the root domain
  # "staging.mindroom.chat" - "mindroom.chat" = "staging"
  # "mindroom.chat" - "mindroom.chat" = ""
  subdomain_prefix = var.superdomain == var.root_domain ? "" : trimspace(trimsuffix(var.superdomain, ".${var.root_domain}"))

  platform_subdomains = ["app", "api", "webhooks"]
}

resource "porkbun_dns_record" "platform_a" {
  for_each = toset(local.platform_subdomains)
  domain   = var.root_domain
  name     = local.subdomain_prefix != "" ? "${each.value}.${local.subdomain_prefix}" : each.value
  type     = "A"
  content  = var.ipv4_address
  ttl      = "600"
}

resource "porkbun_dns_record" "platform_aaaa" {
  for_each = toset(local.platform_subdomains)
  domain   = var.root_domain
  name     = local.subdomain_prefix != "" ? "${each.value}.${local.subdomain_prefix}" : each.value
  type     = "AAAA"
  content  = var.ipv6_address
  ttl      = "600"
}

resource "porkbun_dns_record" "apex_a" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? local.subdomain_prefix : ""
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "apex_aaaa" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? local.subdomain_prefix : ""
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_a" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.${local.subdomain_prefix}" : "*"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_aaaa" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.${local.subdomain_prefix}" : "*"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_api_a" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.api.${local.subdomain_prefix}" : "*.api"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_api_aaaa" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.api.${local.subdomain_prefix}" : "*.api"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_matrix_a" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.matrix.${local.subdomain_prefix}" : "*.matrix"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_matrix_aaaa" {
  domain  = var.root_domain
  name    = local.subdomain_prefix != "" ? "*.matrix.${local.subdomain_prefix}" : "*.matrix"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}
