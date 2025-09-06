# MindRoom SaaS Platform - Deployment Information

## 🚀 Infrastructure Successfully Deployed!

**Deployment Date:** 2025-09-04

## Server Information

To get current server information, run these terraform commands:

```bash
cd infrastructure/terraform

# Dokku Server
terraform output -raw dokku_server_ip       # IPv4 address
terraform output -raw dokku_server_ipv6     # IPv6 address
terraform output -raw ssh_command_dokku     # SSH command
terraform output -raw dokku_admin_password  # Admin password

# Platform Server
terraform output -raw platform_server_ip    # IPv4 address
terraform output -raw ssh_command_platform  # SSH command

# DNS Instructions
terraform output dns_instructions           # Full DNS setup
```

### Server Types
- **Dokku Server:** CPX31 (4 vCPU, 8GB RAM) with 200GB volume
- **Platform Server:** CPX21 (3 vCPU, 4GB RAM) with 50GB volume

## DNS Configuration Required

Get the current DNS configuration by running:

```bash
cd infrastructure/terraform
terraform output dns_instructions
```

This will show you the exact A and AAAA records to configure for your domain.

## Services Status

### Cloud-Init Progress
Both servers are currently running their cloud-init scripts which will:
1. Install Docker and required software
2. Set up Dokku (on Dokku server)
3. Configure Nginx and SSL (on Platform server)
4. Set up security (firewall, fail2ban)
5. Mount storage volumes
6. Reboot when complete

**Estimated completion time:** 10-15 minutes

### Platform Services
The following services will be available after cloud-init completes:
- **Stripe Handler:** https://webhooks.mindroom.chat/stripe
- **Dokku Provisioner API:** https://api.mindroom.chat/provision
- **Customer Portal:** https://app.mindroom.chat
- **Admin Dashboard:** https://admin.mindroom.chat

## Next Steps

1. **Wait for servers to complete setup** (10-15 minutes)
   - Servers will reboot automatically when ready

2. **Configure DNS records** (see above)
   - Add all A and AAAA records
   - Wait for propagation (5-30 minutes)

3. **Verify server access**
   ```bash
   # Get SSH commands from terraform
   cd infrastructure/terraform

   # Test Dokku server
   $(terraform output -raw ssh_command_dokku)
   dokku apps:list

   # Test platform server
   $(terraform output -raw ssh_command_platform)
   docker ps
   ```

4. **Complete SSL setup** (after DNS is configured)
   ```bash
   # SSH to platform server (get IP from terraform output)
   $(cd infrastructure/terraform && terraform output -raw ssh_command_platform)

   # Run certbot
   certbot certonly --nginx --non-interactive --agree-tos \
     --email admin@mindroom.chat \
     -d mindroom.chat -d app.mindroom.chat -d admin.mindroom.chat \
     -d api.mindroom.chat -d webhooks.mindroom.chat
   systemctl restart nginx
   ```

5. **Deploy platform services code**
   - Deploy the 4 services from the platform agents output:
     - Stripe Handler (Node.js)
     - Dokku Provisioner (Python/FastAPI)
     - Customer Portal (Next.js)
     - Admin Dashboard (React)

6. **Configure external services**
   - Set up Supabase project and run migrations
   - Configure Stripe account and create products
   - Update terraform.tfvars with real credentials
   - Re-run terraform apply to update configuration

## Important Security Notes

1. **ROTATE THE HETZNER API KEY** (if not done already)
   - The key was exposed in git history
   - Generate a new one at https://console.hetzner.cloud/

2. **Change default passwords after setup:**
   - Dokku admin password
   - Admin dashboard password

3. **Restrict SSH access:**
   - Update `admin_ips` in terraform.tfvars to your IP
   - Run `terraform apply` to update firewall rules

## Terraform Management

```bash
# View current state
cd infrastructure/terraform
terraform show

# Update infrastructure
terraform plan
terraform apply

# Destroy (BE CAREFUL!)
terraform destroy
```

## Monitoring

- Hetzner Cloud Console: https://console.hetzner.cloud/
- Server metrics available in Hetzner dashboard
- Application logs via SSH to servers

## Support Resources

- Dokku Documentation: https://dokku.com/docs
- Hetzner Cloud: https://docs.hetzner.com/cloud
- Platform Services: See individual agent documentation
