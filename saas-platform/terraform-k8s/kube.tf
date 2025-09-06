terraform {
  required_version = ">= 1.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
    porkbun = {
      source  = "cullenmcdermott/porkbun"
      version = "~> 0.2"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.24"
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

module "kube-hetzner" {
  source = "kube-hetzner/kube-hetzner/hcloud"
  version = "2.15.0"

  providers = {
    hcloud = hcloud
  }

  hcloud_token = var.hcloud_token

  # Network configuration - EU Central
  network_region = "eu-central"

  # Cluster name
  cluster_name = var.cluster_name

  # SSH key configuration - use dedicated cluster key
  ssh_public_key = file("./cluster_ssh_key.pub")
  ssh_private_key = file("./cluster_ssh_key")

  # Single node configuration - everything runs on one node
  control_plane_nodepools = [
    {
      name        = "control-plane"
      server_type = var.server_type
      location    = var.location
      labels      = []
      taints      = []  # No taints - allow workloads to run
      count       = 1
    }
  ]

  # No separate agent nodes for single-node setup
  agent_nodepools = [{
    name        = "dummy"
    server_type = var.server_type
    location    = var.location
    labels      = []
    taints      = []
    count       = 0  # No nodes will be created
  }]

  # Allow scheduling on control plane (required for single node)
  allow_scheduling_on_control_plane = true

  # Disable automatic OS upgrades for single node
  automatically_upgrade_os = false

  # K3s configuration
  initial_k3s_channel = "v1.31"

  # CNI - use default flannel
  cni_plugin = "flannel"

  # Enable basic services
  enable_metrics_server = true

  # Ingress controller - nginx
  ingress_controller = "nginx"

  # Enable cert-manager for SSL
  enable_cert_manager = true

  # Storage - Longhorn
  enable_longhorn = true
  longhorn_replica_count = 1  # Single replica for single node

  # Disable Rancher UI for now
  enable_rancher = false

  # Create kubeconfig file
  create_kubeconfig = true

  # Fix kured version due to renamed manifest in v1.20.0
  # See: https://github.com/kube-hetzner/terraform-hcloud-kube-hetzner/issues/1887
  kured_version = "1.19.0"

  # Firewall settings - be careful with these in production!
  firewall_kube_api_source = ["0.0.0.0/0", "::/0"]  # Open to all for now
  firewall_ssh_source = ["0.0.0.0/0", "::/0"]  # Open to all for now

  # Use existing network (optional - comment out if not needed)
  # existing_network_id = []

  # Post-install commands (optional)
  # postinstall_exec = []
}

# ===========================================
# Outputs
# ===========================================

output "cluster_ip" {
  value       = module.kube-hetzner.control_plane_nodes[0].ipv4_address
  description = "IPv4 address of the K3s cluster"
}

output "cluster_ipv6" {
  value       = module.kube-hetzner.control_plane_nodes[0].ipv6_address
  description = "IPv6 address of the K3s cluster"
}

output "kubeconfig_path" {
  value       = "${path.module}/${var.cluster_name}_kubeconfig.yaml"
  description = "Path to the kubeconfig file"
}
