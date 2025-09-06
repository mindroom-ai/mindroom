# Post-provisioning automation using Terraform
# This handles cross-server configuration that requires both servers to be running

# Wait for both servers to be ready and exchange SSH keys
resource "null_resource" "ssh_key_exchange" {
  depends_on = [
    hcloud_server.platform,
    hcloud_server.dokku
  ]

  # Trigger on server changes
  triggers = {
    platform_id = hcloud_server.platform.id
    dokku_id    = hcloud_server.dokku.id
  }

  # Wait for platform server to generate its SSH key
  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = hcloud_server.platform.ipv4_address
      user        = "root"
      private_key = file(var.ssh_private_key_path)
    }

    inline = [
      "# Wait for cloud-init to complete",
      "cloud-init status --wait",
      "# Wait for SSH key generation",
      "for i in {1..30}; do",
      "  if [ -f /opt/platform/dokku-ssh-key.pub ]; then",
      "    echo 'SSH key found'",
      "    break",
      "  fi",
      "  echo 'Waiting for SSH key generation...'",
      "  sleep 2",
      "done",
      "# Display the public key",
      "cat /opt/platform/dokku-ssh-key.pub"
    ]
  }

  # Get the platform's SSH key locally first
  provisioner "local-exec" {
    command = <<-EOT
      # Get the platform's SSH key and save it locally
      ssh -o StrictHostKeyChecking=no -i ${var.ssh_private_key_path} root@${hcloud_server.platform.ipv4_address} \
        'cat /opt/platform/dokku-ssh-key.pub' > /tmp/platform_ssh_key.pub
    EOT
  }

  # Add platform's SSH key to Dokku server
  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = hcloud_server.dokku.ipv4_address
      user        = "root"
      private_key = file(var.ssh_private_key_path)
    }

    inline = [
      "# Wait for cloud-init to complete",
      "cloud-init status --wait",
      "# Add platform's SSH key to authorized_keys",
      "mkdir -p /root/.ssh",
      "chmod 700 /root/.ssh",
      "touch /root/.ssh/authorized_keys",
      "chmod 600 /root/.ssh/authorized_keys"
    ]
  }

  # Copy the SSH key to dokku server
  provisioner "file" {
    connection {
      type        = "ssh"
      host        = hcloud_server.dokku.ipv4_address
      user        = "root"
      private_key = file(var.ssh_private_key_path)
    }

    source      = "/tmp/platform_ssh_key.pub"
    destination = "/tmp/platform_ssh_key.pub"
  }

  # Add the key to authorized_keys
  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = hcloud_server.dokku.ipv4_address
      user        = "root"
      private_key = file(var.ssh_private_key_path)
    }

    inline = [
      "# Add the key to authorized_keys",
      "cat /tmp/platform_ssh_key.pub >> /root/.ssh/authorized_keys",
      "rm /tmp/platform_ssh_key.pub",
      "echo 'Platform SSH key added successfully'"
    ]
  }
}

# Create environment file on platform server with all necessary variables
resource "null_resource" "platform_env_setup" {
  depends_on = [
    hcloud_server.platform,
    hcloud_server.dokku,
    null_resource.ssh_key_exchange
  ]

  triggers = {
    platform_id = hcloud_server.platform.id
    env_hash    = md5(jsonencode({
      supabase_url           = var.supabase_url
      stripe_secret_key      = var.stripe_secret_key
      stripe_webhook_secret  = var.stripe_webhook_secret
      hcloud_token          = var.hcloud_token
    }))
  }

  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = hcloud_server.platform.ipv4_address
      user        = "root"
      private_key = file(var.ssh_private_key_path)
    }

    inline = [
      "# Create complete .env file",
      "cat > /root/.env <<'EOF'",
      "NODE_ENV=production",
      "API_URL=https://api.${var.domain}",
      "APP_URL=https://app.${var.domain}",
      "ADMIN_URL=https://admin.${var.domain}",
      "WEBHOOK_URL=https://webhooks.${var.domain}",
      "SUPABASE_URL=${var.supabase_url}",
      "SUPABASE_SERVICE_KEY=${var.supabase_service_key}",
      "STRIPE_SECRET_KEY=${var.stripe_secret_key}",
      "STRIPE_WEBHOOK_SECRET=${var.stripe_webhook_secret}",
      "DOKKU_HOST=${hcloud_server.dokku.ipv4_address}",
      "DOKKU_SSH_PORT=22",
      "DOKKU_USER=root",
      "HCLOUD_TOKEN=${var.hcloud_token}",
      "GITEA_TOKEN=${var.gitea_token}",
      "EOF",
      "# Copy to platform directory",
      "cp /root/.env /opt/platform/.env",
      "chmod 600 /opt/platform/.env",
      "echo 'Environment setup complete'"
    ]
  }
}

# Output to indicate post-provisioning is complete
output "post_provisioning_complete" {
  value = {
    ssh_keys_exchanged = null_resource.ssh_key_exchange.id != "" ? true : false
    env_configured     = null_resource.platform_env_setup.id != "" ? true : false
    ready_for_deployment = true
  }
  description = "Status of post-provisioning tasks"
}
