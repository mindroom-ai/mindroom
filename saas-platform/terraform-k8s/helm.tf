# ===========================================
# Kubernetes and Helm Providers
# ===========================================

# Configure Kubernetes provider to use the cluster we just created
provider "kubernetes" {
  config_path = "${path.module}/${var.cluster_name}_kubeconfig.yaml"
}

# Configure Helm provider
provider "helm" {
  kubernetes {
    config_path = "${path.module}/${var.cluster_name}_kubeconfig.yaml"
  }
}

# Configure kubectl provider
provider "kubectl" {
  config_path = "${path.module}/${var.cluster_name}_kubeconfig.yaml"
}

# ===========================================
# Wait for cluster to be ready
# ===========================================

resource "time_sleep" "wait_for_cluster" {
  depends_on = [module.kube-hetzner]

  create_duration = "30s"
}

# ===========================================
# Deploy MindRoom Platform
# ===========================================

# Create values for the Helm chart
locals {
  platform_values = {
    environment = var.environment
    domain      = local.dns_domain
    registry    = var.registry
    imageTag    = var.image_tag
    replicas    = 1

    supabase = {
      url        = var.supabase_url
      anonKey    = var.supabase_anon_key
      serviceKey = var.supabase_service_key
    }

    stripe = {
      publishableKey = var.stripe_publishable_key
      secretKey      = var.stripe_secret_key
      webhookSecret  = var.stripe_webhook_secret
    }

    gitea = {
      token = var.gitea_token
    }
  }
}

# Deploy the platform Helm chart
resource "helm_release" "mindroom_platform" {
  depends_on = [
    time_sleep.wait_for_cluster,
    kubectl_manifest.cluster_issuer_prod,
    kubectl_manifest.cluster_issuer_staging
  ]

  name       = "mindroom-${var.environment}"
  namespace  = var.environment
  chart      = "${path.module}/../k8s/platform"

  create_namespace = true
  wait             = true
  timeout          = 600

  values = [
    yamlencode(local.platform_values)
  ]
}

# ===========================================
# Outputs
# ===========================================

output "platform_status" {
  value = {
    status    = "✅ Platform deployed"
    namespace = var.environment
    release   = helm_release.mindroom_platform.name
    urls      = {
      app      = "https://app.${local.dns_domain}"
      admin    = "https://admin.${local.dns_domain}"
      api      = "https://api.${local.dns_domain}"
      webhooks = "https://webhooks.${local.dns_domain}"
    }
  }
  description = "Platform deployment status"
}
