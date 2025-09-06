# ⚠️ SECURITY WARNING

## SSH Access Currently OPEN TO EVERYONE

Your `terraform.tfvars` has:
```
admin_ips = ["0.0.0.0/0", "::/0"]
```

This allows **ANYONE on the internet** to attempt SSH access to your servers!

## To Fix This

1. Find your current IP address:
   ```bash
   curl ifconfig.me
   ```

2. Update terraform.tfvars:
   ```hcl
   admin_ips = [
     "YOUR.IP.ADDRESS/32",    # Replace with your actual IP
     # Remove the 0.0.0.0/0 and ::/0 lines!
   ]
   ```

3. Apply the change:
   ```bash
   cd infrastructure/terraform
   terraform apply
   ```

## Other Security Notes

1. **Hetzner API Token** is visible in terraform.tfvars
   - Consider using environment variables instead
   - Or ensure this file is NEVER committed to git

2. **Supabase and Stripe keys** are also in the file
   - These should ideally be in environment variables
   - Make sure terraform.tfvars is in .gitignore

## Check .gitignore

Run this to verify terraform.tfvars is ignored:
```bash
git check-ignore infrastructure/terraform/terraform.tfvars
```

If it returns nothing, add it to .gitignore immediately!
