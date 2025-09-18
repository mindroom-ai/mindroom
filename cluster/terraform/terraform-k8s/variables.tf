# ===========================================
# Core Variables
# ===========================================

variable "hcloud_token" {
  description = "Hetzner Cloud API token"
  type        = string
  sensitive   = true
}

variable "domain" {
  description = "Full domain where platform is deployed (e.g., staging.mindroom.chat)"
  type        = string
  default     = "mindroom.chat"
}

variable "root_domain" {
  description = "Root domain registered with DNS provider (e.g., mindroom.chat)"
  type        = string
  default     = "mindroom.chat"
}

variable "environment" {
  description = "Environment (staging or production)"
  type        = string
  default     = "test"
}


variable "deploy_platform" {
  description = "Whether to deploy the MindRoom platform via Helm"
  type        = bool
  default     = false
}

variable "deploy_monitoring" {
  description = "Whether to deploy the monitoring stack (Prometheus)"
  type        = bool
  default     = true
}

variable "monitoring_release_name" {
  description = "Helm release name for the monitoring stack"
  type        = string
  default     = "monitoring"
}

variable "monitoring_namespace" {
  description = "Namespace where the monitoring stack should live"
  type        = string
  default     = "monitoring"
}

variable "monitoring_chart_version" {
  description = "kube-prometheus-stack chart version to deploy"
  type        = string
  default     = "75.15.1"
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

variable "cleanup_scheduler_enabled" {
  description = "Whether the cleanup scheduler should run (true/false)"
  type        = string
  default     = "false"
}

# API Keys for AI Models
variable "openai_api_key" {
  description = "OpenAI API key for MindRoom instances"
  type        = string
  sensitive   = true
  default     = ""
}

variable "anthropic_api_key" {
  description = "Anthropic API key for MindRoom instances"
  type        = string
  sensitive   = true
  default     = ""
}

variable "openrouter_api_key" {
  description = "OpenRouter API key for MindRoom instances"
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_api_key" {
  description = "Google API key for MindRoom instances"
  type        = string
  sensitive   = true
  default     = ""
}

variable "deepseek_api_key" {
  description = "DeepSeek API key for MindRoom instances"
  type        = string
  sensitive   = true
  default     = ""
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
