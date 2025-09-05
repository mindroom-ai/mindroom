variable "hcloud_token" {
  description = "Hetzner Cloud API Token"
  type        = string
  sensitive   = true
}

variable "environment" {
  description = "Environment name (staging/production)"
  type        = string
  default     = "production"
}

variable "domain" {
  description = "Main domain for the platform"
  type        = string
  default     = "mindroom.chat"
}

variable "location" {
  description = "Server location"
  type        = string
  default     = "nbg1" # Nuremberg, Germany (good for EU/US)
}

# Server configurations
variable "dokku_server_type" {
  description = "Server type for Dokku instances"
  type        = string
  default     = "cpx31" # 4 vCPU, 8GB RAM - good for ~20 customer instances
}

variable "platform_server_type" {
  description = "Server type for platform services"
  type        = string
  default     = "cpx21" # 3 vCPU, 4GB RAM - sufficient for platform services
}

# Storage
variable "dokku_volume_size" {
  description = "Size of Dokku data volume in GB"
  type        = number
  default     = 200 # Enough for many customer instances
}

variable "platform_volume_size" {
  description = "Size of platform data volume in GB"
  type        = number
  default     = 50 # Platform services data
}

# SSH Keys
variable "ssh_public_key_path" {
  description = "Path to SSH public key for admin access"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "dokku_provisioner_key_path" {
  description = "Path to SSH public key for Dokku provisioner"
  type        = string
  default     = "./ssh/dokku_provisioner.pub"
}

variable "dokku_provisioner_private_key_path" {
  description = "Path to SSH private key for Dokku provisioner"
  type        = string
  default     = "./ssh/dokku_provisioner"
  sensitive   = true
}

# Security
variable "admin_ips" {
  description = "List of IP addresses allowed to SSH to platform server"
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"] # Change this to your IP for production!
}

variable "enable_backups" {
  description = "Enable automatic backups"
  type        = bool
  default     = true # Recommended for production
}

# Software versions
variable "dokku_version" {
  description = "Dokku version to install"
  type        = string
  default     = "v0.32.3"
}

# Supabase Configuration (from your Supabase project)
variable "supabase_url" {
  description = "Supabase project URL"
  type        = string
  sensitive   = false
}

variable "supabase_service_key" {
  description = "Supabase service role key"
  type        = string
  sensitive   = true
}

variable "supabase_anon_key" {
  description = "Supabase anonymous key"
  type        = string
  sensitive   = false
}

# Stripe Configuration
variable "stripe_secret_key" {
  description = "Stripe secret key"
  type        = string
  sensitive   = true
}

variable "stripe_publishable_key" {
  description = "Stripe publishable key"
  type        = string
  sensitive   = false
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook endpoint secret"
  type        = string
  sensitive   = true
}

# Price IDs from Stripe
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

# Email Configuration (optional)
variable "resend_api_key" {
  description = "Resend API key for email sending"
  type        = string
  default     = ""
  sensitive   = true
}
