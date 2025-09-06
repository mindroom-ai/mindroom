# Cloud-Init Configuration

This directory contains cloud-init configurations for provisioning the MindRoom SaaS platform servers.

## Structure

We use cloud-config YAML format for cleaner, more maintainable server provisioning:

```
cloud-init/
├── platform.yaml    # Platform management server configuration
├── dokku.yaml      # Dokku customer instances server configuration
└── README.md       # This file
```

## Benefits of Cloud-Config YAML

1. **Cleaner Syntax**: YAML is more readable than embedded bash scripts
2. **Native CloudInit Features**: Direct access to all cloud-init modules
3. **Better Error Handling**: Cloud-init validates YAML and provides better error messages
4. **Separation of Concerns**: Scripts are separated into logical files within the YAML
5. **Template Variables**: Terraform can still inject variables via `templatefile()`

## Platform Server (platform.yaml)

Configures the management server with:
- Docker and Docker Compose
- Nginx reverse proxy
- Platform services (customer portal, admin dashboard, stripe handler, dokku provisioner)
- Firewall (UFW) and security (fail2ban)
- System optimizations

### Key Scripts Created:
- `/opt/platform/deploy-services.sh` - Deploys Docker containers
- `/opt/platform/check-status.sh` - Checks service health
- `/opt/platform/scripts/mount-volume.sh` - Mounts Hetzner storage volume
- `/opt/platform/scripts/setup-platform.sh` - Platform-specific setup
- `/opt/platform/scripts/setup-nginx.sh` - Nginx configuration

## Dokku Server (dokku.yaml)

Configures the Dokku PaaS server with:
- Docker
- Dokku with plugins (postgres, redis, letsencrypt, resource-limit)
- Firewall and security
- User permissions for automated deployments

### Key Scripts Created:
- `/root/add-platform-key.sh` - Adds platform server's SSH key
- `/opt/dokku/scripts/mount-volume.sh` - Mounts storage and moves Docker/Dokku data
- `/opt/dokku/scripts/setup-dokku.sh` - Dokku-specific configuration

## Terraform Integration

The YAML files are used in Terraform via `templatefile()`:

```hcl
resource "hcloud_server" "platform" {
  # ...
  user_data = templatefile("${path.module}/cloud-init/platform.yaml", {
    domain                = var.domain
    supabase_url         = var.supabase_url
    supabase_service_key = var.supabase_service_key
    stripe_secret_key    = var.stripe_secret_key
    stripe_webhook_secret = var.stripe_webhook_secret
    dokku_host           = hcloud_server.dokku.ipv4_address
    registry             = "git.nijho.lt/basnijholt"
    arch                 = "amd64"
  })
}
```

## Cloud-Init Execution Order

1. **bootcmd**: Runs very early (before network is up)
2. **package_update/upgrade**: Updates system packages
3. **packages**: Installs required packages
4. **write_files**: Creates configuration files and scripts
5. **runcmd**: Executes setup commands
6. **final_message**: Shows completion message

## Debugging

To check cloud-init status on a server:
```bash
# Check status
cloud-init status

# View logs
cat /var/log/cloud-init.log
cat /var/log/cloud-init-output.log

# Re-run cloud-init (careful!)
cloud-init clean
cloud-init init
```

## Best Practices

1. **Use write_files for scripts**: Instead of embedding scripts in runcmd
2. **Set proper permissions**: Always set owner and permissions for sensitive files
3. **Use bootcmd sparingly**: Only for critical early-boot configuration
4. **Template variables carefully**: Escape `$` as `$` when needed in scripts
5. **Test locally**: Use multipass or similar to test cloud-config locally

## Testing

You can validate the YAML syntax locally:
```bash
cloud-init schema --config-file platform.yaml
cloud-init schema --config-file dokku.yaml
```
