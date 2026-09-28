# ===========================================
# cert-manager SSL Configuration
# ===========================================
# This configures Let's Encrypt SSL certificates for MindRoom instances
# Uses HTTP-01 challenge for automatic certificate issuance

# Wait for cert-manager to be ready (installed by kube-hetzner module)
resource "time_sleep" "wait_for_cert_manager" {
  depends_on = [module.kube-hetzner]

  create_duration = "60s"
}

# Create namespace for instances
resource "kubernetes_namespace" "mindroom_instances" {
  metadata {
    name = "mindroom-instances"
    # Tenant pods run tenant code on the node that hosts the control plane, so admission rejects
    # privileged containers, host namespaces, hostPath volumes, and capabilities beyond the default set.
    labels = {
      "pod-security.kubernetes.io/enforce" = "baseline"
    }
  }

  depends_on = [module.kube-hetzner]
}

# Apply ClusterIssuer for Let's Encrypt Production
resource "kubectl_manifest" "cluster_issuer_prod" {
  yaml_body = file("${path.module}/manifests/cert-manager/cluster-issuer-prod.yaml")

  depends_on = [
    time_sleep.wait_for_cert_manager
  ]
}

# Apply ClusterIssuer for Let's Encrypt Staging (for testing)
resource "kubectl_manifest" "cluster_issuer_staging" {
  yaml_body = file("${path.module}/manifests/cert-manager/cluster-issuer-staging.yaml")

  depends_on = [
    time_sleep.wait_for_cert_manager
  ]
}

# Output status
output "ssl_configuration" {
  value = {
    status = "✅ SSL configured with Let's Encrypt"
    info   = "Certificates will be automatically issued for each instance"
  }
  description = "SSL certificate configuration status"
}
