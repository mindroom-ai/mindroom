# ===========================================
# Kubernetes and Helm Providers
# ===========================================

locals {
  # Full superdomain (e.g., staging.mindroom.chat or mindroom.chat)
  dns_domain = var.domain
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
      publishableKey = var.stripe_publishable_key
      secretKey      = var.stripe_secret_key
      webhookSecret  = var.stripe_webhook_secret
    }

    provisioner = {
      apiKey = var.provisioner_api_key
    }

    gitea = {
      user  = var.gitea_user
      token = var.gitea_token
    }

    apiKeys = {
      openai     = var.openai_api_key
      anthropic  = var.anthropic_api_key
      openrouter = var.openrouter_api_key
      google     = var.google_api_key
      deepseek   = var.deepseek_api_key
    }

    cleanupScheduler = {
      enabled = var.cleanup_scheduler_enabled
    }

    monitoring = {
      enabled     = var.deploy_monitoring
      releaseLabel = var.monitoring_release_name
    }
  }
}

# ===========================================
# Deploy Monitoring Stack
# ===========================================

resource "helm_release" "monitoring_stack" {
  count = var.deploy_monitoring ? 1 : 0

  depends_on = [
    time_sleep.wait_for_cluster
  ]

  name       = var.monitoring_release_name
  namespace  = var.monitoring_namespace
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.monitoring_chart_version

  create_namespace = true
  wait             = true
  timeout          = 600

  values = [
    yamlencode({
      grafana = {
        enabled = false
      }
      prometheus = {
        prometheusSpec = {
          serviceMonitorSelectorNilUsesHelmValues = false
          serviceMonitorSelector = {
            matchLabels = {
              release = var.monitoring_release_name
            }
          }
          serviceMonitorNamespaceSelector = {
            any = true
          }
          ruleSelectorNilUsesHelmValues = false
          ruleSelector = {
            matchLabels = {
              release = var.monitoring_release_name
            }
          }
          ruleNamespaceSelector = {
            any = true
          }
        }
      }
      alertmanager = {
        alertmanagerSpec = {
          replicas = 1
        }
      }
    })
  ]
}

# Deploy the platform Helm chart
resource "helm_release" "mindroom_platform" {
  count = var.deploy_platform ? 1 : 0
  depends_on = [
    time_sleep.wait_for_cluster,
    kubectl_manifest.cluster_issuer_prod,
    kubectl_manifest.cluster_issuer_staging
  ]

  name      = "mindroom-${var.environment}"
  namespace = var.environment
  # Charts live at cluster/k8s/platform relative to this module
  chart = "${path.module}/../../k8s/platform"

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
    urls = {
      app      = "https://app.${local.dns_domain}"
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
