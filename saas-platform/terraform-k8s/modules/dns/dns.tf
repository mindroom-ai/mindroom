terraform {
  required_providers {
    porkbun = {
      source  = "cullenmcdermott/porkbun"
      version = "~> 0.2"
    }
  }
}


variable "domain" { type = string }
variable "environment" { type = string }
variable "porkbun_api_key" { type = string }
variable "porkbun_secret_key" { type = string }
variable "ipv4_address" { type = string }
variable "ipv6_address" { type = string }

locals {
  dns_domain = var.environment == "production" ? var.domain : "${var.environment}.${var.domain}"
  platform_subdomains = ["app", "admin", "api", "webhooks"]
}

resource "porkbun_dns_record" "platform_a" {
  for_each = toset(local.platform_subdomains)
  domain   = var.domain
  name     = var.environment == "production" ? each.value : "${each.value}.${var.environment}"
  type     = "A"
  content  = var.ipv4_address
  ttl      = "600"
}

resource "porkbun_dns_record" "platform_aaaa" {
  for_each = toset(local.platform_subdomains)
  domain   = var.domain
  name     = var.environment == "production" ? each.value : "${each.value}.${var.environment}"
  type     = "AAAA"
  content  = var.ipv6_address
  ttl      = "600"
}

resource "porkbun_dns_record" "wildcard_a" {
  domain  = var.domain
  name    = var.environment == "production" ? "*" : "*.${var.environment}"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_aaaa" {
  domain  = var.domain
  name    = var.environment == "production" ? "*" : "*.${var.environment}"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_api_a" {
  domain  = var.domain
  name    = var.environment == "production" ? "*.api" : "*.api.${var.environment}"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_api_aaaa" {
  domain  = var.domain
  name    = var.environment == "production" ? "*.api" : "*.api.${var.environment}"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_matrix_a" {
  domain  = var.domain
  name    = var.environment == "production" ? "*.matrix" : "*.matrix.${var.environment}"
  type    = "A"
  content = var.ipv4_address
  ttl     = "600"
}

resource "porkbun_dns_record" "wildcard_matrix_aaaa" {
  domain  = var.domain
  name    = var.environment == "production" ? "*.matrix" : "*.matrix.${var.environment}"
  type    = "AAAA"
  content = var.ipv6_address
  ttl     = "600"
}
