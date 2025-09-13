# ===========================================
# Kubernetes and Helm Providers
# ===========================================

locals {
  dns_domain = var.environment == "production" ? var.domain : "${var.environment}.${var.domain}"
}

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
      publishableKey      = var.stripe_publishable_key
      secretKey           = var.stripe_secret_key
      webhookSecret       = var.stripe_webhook_secret
      priceStarter        = var.stripe_price_starter
      priceProfessional   = var.stripe_price_professional
      priceEnterprise     = var.stripe_price_enterprise
    }

    provisioner = {
      apiKey = var.provisioner_api_key
    }

    gitea = {
      user  = var.gitea_user
      token = var.gitea_token
    }
  }
}

# Deploy the platform Helm chart
resource "helm_release" "mindroom_platform" {
  count = var.deploy_platform ? 1 : 0
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
  value = var.deploy_platform ? {
    status    = "✅ Platform deployed"
    namespace = var.environment
    release   = helm_release.mindroom_platform[0].name
    urls      = {
      app      = "https://app.${local.dns_domain}"
      admin    = "https://admin.${local.dns_domain}"
      api      = "https://api.${local.dns_domain}"
      webhooks = "https://webhooks.${local.dns_domain}"
    }
  } : {
    status    = "ℹ️ Platform deployment skipped (deploy_platform=false)"
    namespace = var.environment
    release   = ""
    urls      = {}
  }
  description = "Platform deployment status"
}
