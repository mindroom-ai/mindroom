terraform {
  required_version = ">= 1.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.4"
    }
  }

  # Optional: Use Terraform Cloud for state management
  # backend "remote" {
  #   organization = "mindroom"
  #   workspaces {
  #     name = "mindroom-infrastructure"
  #   }
  # }
}

# Configure Hetzner Cloud Provider
provider "hcloud" {
  token = var.hcloud_token
}

# ===========================================
# SSH Keys
# ===========================================

# Create SSH key for Dokku server access
resource "hcloud_ssh_key" "dokku_admin" {
  name       = "mindroom-dokku-admin"
  public_key = file(var.ssh_public_key_path)
}

# Create SSH key for provisioner to access Dokku
resource "hcloud_ssh_key" "dokku_provisioner" {
  name       = "mindroom-dokku-provisioner"
  public_key = file(var.dokku_provisioner_key_path)
}

# ===========================================
# Network Setup
# ===========================================

# Create private network for internal communication
resource "hcloud_network" "main" {
  name     = "mindroom-network"
  ip_range = "10.0.0.0/16"

  labels = {
    project     = "mindroom"
    environment = var.environment
  }
}

# Create subnet
resource "hcloud_network_subnet" "main" {
  network_id   = hcloud_network.main.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "10.0.1.0/24"
}

# ===========================================
# Firewall Rules
# ===========================================

# Firewall for Dokku server
resource "hcloud_firewall" "dokku" {
  name = "mindroom-dokku-firewall"

  # SSH
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "22"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # HTTP
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "80"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # HTTPS
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "443"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # Matrix federation port (if needed)
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "8448"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  labels = {
    project     = "mindroom"
    environment = var.environment
  }
}

# Firewall for platform services
resource "hcloud_firewall" "platform" {
  name = "mindroom-platform-firewall"

  # SSH
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "22"
    source_ips = var.admin_ips
  }

  # HTTP
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "80"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  # HTTPS
  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "443"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  labels = {
    project     = "mindroom"
    environment = var.environment
  }
}

# ===========================================
# Dokku Server
# ===========================================

# Generate random password for Dokku admin
resource "random_password" "dokku_admin" {
  length  = 32
  special = true
}

# Main Dokku server for customer instances
resource "hcloud_server" "dokku" {
  name        = "${var.environment}-dokku-instances"
  server_type = var.dokku_server_type
  image       = "ubuntu-22.04"
  location    = var.location

  ssh_keys = [
    hcloud_ssh_key.dokku_admin.id,
    hcloud_ssh_key.dokku_provisioner.id
  ]

  firewall_ids = [hcloud_firewall.dokku.id]

  backups = var.enable_backups

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  labels = {
    project     = "mindroom"
    environment = var.environment
    role        = "dokku"
    service     = "customer-instances"
  }

  user_data = templatefile("${path.module}/cloud-init/dokku.sh", {
    dokku_version       = var.dokku_version
    dokku_domain       = var.domain
    admin_password     = random_password.dokku_admin.result
    provisioner_pub_key = file(var.dokku_provisioner_key_path)
  })
}

# Attach to private network
resource "hcloud_server_network" "dokku" {
  server_id  = hcloud_server.dokku.id
  network_id = hcloud_network.main.id
  ip         = "10.0.1.10"
}

# ===========================================
# Platform Services Server
# ===========================================

# Server for platform services (Stripe handler, provisioner, etc.)
resource "hcloud_server" "platform" {
  name        = "${var.environment}-platform"
  server_type = var.platform_server_type
  image       = "ubuntu-22.04"
  location    = var.location

  ssh_keys = [hcloud_ssh_key.dokku_admin.id]

  firewall_ids = [hcloud_firewall.platform.id]

  backups = var.enable_backups

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  labels = {
    project     = "mindroom"
    environment = var.environment
    role        = "platform"
    service     = "management"
  }

  user_data = templatefile("${path.module}/cloud-init/platform.sh", {
    docker_compose_version = "2.23.0"
    domain                = var.domain
    hcloud_token         = var.hcloud_token
    supabase_url         = var.supabase_url
    supabase_service_key = var.supabase_service_key
    stripe_secret_key    = var.stripe_secret_key
    stripe_webhook_secret = var.stripe_webhook_secret
    dokku_host           = hcloud_server.dokku.ipv4_address
    dokku_ssh_key        = file(var.dokku_provisioner_private_key_path)
  })
}

# Attach to private network
resource "hcloud_server_network" "platform" {
  server_id  = hcloud_server.platform.id
  network_id = hcloud_network.main.id
  ip         = "10.0.1.20"
}

# ===========================================
# Storage Volumes
# ===========================================

# Volume for Dokku persistent data
resource "hcloud_volume" "dokku_data" {
  name     = "${var.environment}-dokku-data"
  size     = var.dokku_volume_size
  location = var.location

  labels = {
    project     = "mindroom"
    environment = var.environment
    service     = "dokku-storage"
  }
}

# Attach volume to Dokku server
resource "hcloud_volume_attachment" "dokku_data" {
  volume_id = hcloud_volume.dokku_data.id
  server_id = hcloud_server.dokku.id
  automount = true
}

# Volume for platform services data
resource "hcloud_volume" "platform_data" {
  name     = "${var.environment}-platform-data"
  size     = var.platform_volume_size
  location = var.location

  labels = {
    project     = "mindroom"
    environment = var.environment
    service     = "platform-storage"
  }
}

# Attach volume to platform server
resource "hcloud_volume_attachment" "platform_data" {
  volume_id = hcloud_volume.platform_data.id
  server_id = hcloud_server.platform.id
  automount = true
}

# ===========================================
# Load Balancer (Optional - for high availability)
# ===========================================

# resource "hcloud_load_balancer" "main" {
#   name               = "${var.environment}-lb"
#   load_balancer_type = "lb11"
#   location          = var.location
#
#   target {
#     type      = "server"
#     server_id = hcloud_server.dokku.id
#   }
#
#   algorithm {
#     type = "round_robin"
#   }
#
#   labels = {
#     project     = "mindroom"
#     environment = var.environment
#   }
# }

# ===========================================
# Outputs
# ===========================================

output "dokku_server_ip" {
  value       = hcloud_server.dokku.ipv4_address
  description = "IPv4 address of the Dokku server"
}

output "dokku_server_ipv6" {
  value       = hcloud_server.dokku.ipv6_address
  description = "IPv6 address of the Dokku server"
}

output "platform_server_ip" {
  value       = hcloud_server.platform.ipv4_address
  description = "IPv4 address of the platform server"
}

output "ssh_command_dokku" {
  value       = "ssh root@${hcloud_server.dokku.ipv4_address}"
  description = "SSH command to connect to Dokku server"
}

output "ssh_command_platform" {
  value       = "ssh root@${hcloud_server.platform.ipv4_address}"
  description = "SSH command to connect to platform server"
}

output "dokku_admin_password" {
  value       = random_password.dokku_admin.result
  sensitive   = true
  description = "Admin password for Dokku web interface"
}

output "dns_instructions" {
  value = <<-EOT

    Please configure the following DNS records for ${var.domain}:

    # Main domain
    A     ${var.domain}                    ${hcloud_server.platform.ipv4_address}
    AAAA  ${var.domain}                    ${hcloud_server.platform.ipv6_address}

    # Platform services
    A     app.${var.domain}                ${hcloud_server.platform.ipv4_address}
    A     admin.${var.domain}              ${hcloud_server.platform.ipv4_address}
    A     api.${var.domain}                ${hcloud_server.platform.ipv4_address}
    A     webhooks.${var.domain}          ${hcloud_server.platform.ipv4_address}

    # Customer instances (wildcard)
    A     *.${var.domain}                  ${hcloud_server.dokku.ipv4_address}
    AAAA  *.${var.domain}                  ${hcloud_server.dokku.ipv6_address}

    # Matrix federation (if using)
    A     *.m.${var.domain}                ${hcloud_server.dokku.ipv4_address}

  EOT
  description = "DNS records to configure"
}

# Save outputs to file for automation
resource "local_file" "outputs" {
  content = jsonencode({
    dokku_server_ip      = hcloud_server.dokku.ipv4_address
    dokku_server_ipv6    = hcloud_server.dokku.ipv6_address
    platform_server_ip   = hcloud_server.platform.ipv4_address
    platform_server_ipv6 = hcloud_server.platform.ipv6_address
    dokku_admin_password = random_password.dokku_admin.result
    private_network_id   = hcloud_network.main.id
    dokku_volume_id      = hcloud_volume.dokku_data.id
    platform_volume_id   = hcloud_volume.platform_data.id
  })
  filename        = "${path.module}/outputs.json"
  file_permission = "0600"
}
