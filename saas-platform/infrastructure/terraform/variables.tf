# ===========================================
# DNS Provider Configuration (Porkbun)
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

variable "enable_matrix_federation" {
  description = "Enable Matrix federation DNS records"
  type        = bool
  default     = true
}

variable "enable_email" {
  description = "Enable email-related DNS records (MX, SPF)"
  type        = bool
  default     = false
}

variable "mx_server" {
  description = "Mail server for MX records (if email is enabled)"
  type        = string
  default     = "mail.example.com"
}

# ===========================================
# Hetzner Cloud Configuration
# ===========================================

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
  default     = "~/.ssh/id_ed25519.pub"
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

# Stripe Configuration
variable "stripe_secret_key" {
  description = "Stripe secret key"
  type        = string
  sensitive   = true
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook endpoint secret"
  type        = string
  sensitive   = true
}
