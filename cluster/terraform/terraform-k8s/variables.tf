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
  default     = "test"
}

variable "enable_dns" {
  description = "Whether to manage DNS records via Porkbun"
  type        = bool
  default     = false
}

variable "deploy_platform" {
  description = "Whether to deploy the MindRoom platform via Helm"
  type        = bool
  default     = false
}

# ===========================================
# DNS Configuration (Porkbun)
# ===========================================

variable "porkbun_api_key" {
  description = "Porkbun API key for DNS management"
  type        = string
  sensitive   = true
  default     = ""
}

variable "porkbun_secret_key" {
  description = "Porkbun secret key for DNS management"
  type        = string
  sensitive   = true
  default     = ""
}

# ===========================================
# Platform Configuration
# ===========================================

variable "supabase_url" {
  description = "Supabase project URL"
  type        = string
  default     = ""
}

variable "supabase_anon_key" {
  description = "Supabase anonymous key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "supabase_service_key" {
  description = "Supabase service role key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_publishable_key" {
  description = "Stripe publishable key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_secret_key" {
  description = "Stripe secret key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_price_starter" {
  description = "Stripe price ID for starter tier"
  type        = string
  default     = ""
}

variable "stripe_price_professional" {
  description = "Stripe price ID for professional tier"
  type        = string
  default     = ""
}

variable "stripe_price_enterprise" {
  description = "Stripe price ID for enterprise tier"
  type        = string
  default     = ""
}

variable "provisioner_api_key" {
  description = "API key for the instance provisioner service"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gitea_user" {
  description = "Gitea username for registry access"
  type        = string
  default     = "basnijholt"
}

variable "gitea_token" {
  description = "Gitea registry token"
  type        = string
  sensitive   = true
  default     = ""
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

# OAuth Provider Variables
variable "google_oauth_client_id" {
  description = "Google OAuth Client ID"
  type        = string
  default     = ""
  sensitive   = true
}

variable "google_oauth_client_secret" {
  description = "Google OAuth Client Secret"
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_oauth_client_id" {
  description = "GitHub OAuth Client ID"
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_oauth_client_secret" {
  description = "GitHub OAuth Client Secret"
  type        = string
  default     = ""
  sensitive   = true
}
