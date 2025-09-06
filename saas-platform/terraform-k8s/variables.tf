# ===========================================
# Core Variables
# ===========================================

variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "domain" {
  description = "Base domain for the platform"
  type        = string
  default     = "mindroom.chat"
}

variable "environment" {
  description = "Environment (staging or production)"
  type        = string
  default     = "staging"
}

# ===========================================
# DNS Configuration (Porkbun)
# ===========================================

variable "porkbun_api_key" {
  description = "Porkbun API key for DNS management"
  type        = string
  sensitive   = true
}

variable "porkbun_secret_key" {
  description = "Porkbun secret key for DNS management"
  type        = string
  sensitive   = true
}

# ===========================================
# Platform Configuration
# ===========================================

variable "supabase_url" {
  description = "Supabase project URL"
  type        = string
}

variable "supabase_anon_key" {
  description = "Supabase anonymous key"
  type        = string
  sensitive   = true
}

variable "supabase_service_key" {
  description = "Supabase service role key"
  type        = string
  sensitive   = true
}

variable "stripe_publishable_key" {
  description = "Stripe publishable key"
  type        = string
  sensitive   = true
}

variable "stripe_secret_key" {
  description = "Stripe secret key"
  type        = string
  sensitive   = true
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook secret"
  type        = string
  sensitive   = true
}

variable "gitea_token" {
  description = "Gitea registry token"
  type        = string
  sensitive   = true
}

variable "registry" {
  description = "Docker registry URL"
  type        = string
  default     = "git.nijho.lt/basnijholt"
}

variable "image_tag" {
  description = "Docker image tag to deploy"
  type        = string
  default     = "latest"
}

# ===========================================
# K3s Configuration
# ===========================================

variable "cluster_name" {
  description = "Name of the K3s cluster"
  type        = string
  default     = "mindroom-k8s"
}

variable "server_type" {
  description = "Hetzner server type"
  type        = string
  default     = "cpx31"
}

variable "location" {
  description = "Hetzner datacenter location"
  type        = string
  default     = "fsn1"
}
