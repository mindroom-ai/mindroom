# 🔐 Matrix Server Security Guide for MindRoom

## Overview

This guide explains how to secure your Matrix server deployment to allow MindRoom bots to register while preventing unauthorized registrations and abuse.

## Quick Start: Secure Deployment

```bash
# Create instance with secure Matrix server
./deploy.py create myapp --domain myapp.com --matrix synapse

# Apply security configuration
./setup-secure-matrix.sh myapp synapse

# Start the instance
./deploy.py start myapp
```

## Security Configurations

### 🛡️ Synapse (Production Recommended)

#### Default Security Settings
- ❌ **Registration Disabled**: Public registration is completely disabled
- ✅ **Shared Secret**: Bots can register using a shared secret
- ✅ **Rate Limiting**: Registration attempts are rate-limited
- ❌ **Guest Access**: Disabled for security
- ✅ **Password Requirements**: Minimum 8 characters enforced

#### Bot Registration Methods

##### Method 1: Shared Secret (Recommended)
```bash
# The shared secret is auto-generated and stored in:
cat instance_data/myapp/.matrix_registration_secret

# Register a bot using the shared secret
docker exec myapp-synapse register_new_matrix_user \
  -u mindroom_calculator \
  -p "secure_password_here" \
  -a \  # Admin user (optional)
  -c /data/homeserver.yaml
```

##### Method 2: Registration Tokens (Most Secure)
```yaml
# In homeserver.yaml, enable:
enable_registration_without_verification: true
registration_requires_token: true
```

Then create tokens:
```bash
# Generate a registration token (one-time use)
docker exec myapp-synapse \
  curl -X POST http://localhost:8008/_synapse/admin/v1/registration_tokens/new \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -d '{"uses_allowed": 1}'
```

##### Method 3: Admin API
```bash
# Using admin account to create users
curl -X PUT http://localhost:8008/_synapse/admin/v2/users/@bot:myapp.com \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -d '{"password": "secure_password", "displayname": "Bot Name"}'
```

### 🦀 Tuwunel (Lightweight Option)

#### Default Security Settings
- ❌ **Registration Disabled**: Set `allow_registration = false`
- ❌ **Guest Access**: Disabled
- ⚠️ **Manual Bot Creation**: Bots must be pre-created

#### Secure Configuration
```toml
# tuwunel-secure.toml
[global]
# CRITICAL: Disable all registration
allow_registration = false
yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse = false
allow_guest_registration = false

# Only allow known servers for federation
federation_domain_whitelist = ["trusted.server.com"]
```

#### Bot Pre-Registration
Since Tuwunel doesn't support shared secret registration, you must:
1. Temporarily enable registration
2. Register all bot accounts
3. Disable registration again

```bash
# Temporarily enable registration (CAREFUL!)
sed -i 's/allow_registration = false/allow_registration = true/' tuwunel.toml
docker restart myapp-tuwunel

# Register bots (quickly!)
# ... register your bots ...

# Disable registration again
sed -i 's/allow_registration = true/allow_registration = false/' tuwunel.toml
docker restart myapp-tuwunel
```

## MindRoom Bot Registration

### Understanding Bot Requirements

MindRoom bots need Matrix accounts for each agent. The system will attempt to register these automatically:

```python
# From src/mindroom/matrix/users.py
username = f"mindroom_{agent_name}"
password = f"{agent_name}_secure_password"
```

### Secure Bot Setup Process

1. **Pre-create bot accounts** (before starting MindRoom):
```bash
# For each agent in your config.yaml
./instance_data/myapp/register-bot.sh http://localhost:8008 mindroom_calculator
./instance_data/myapp/register-bot.sh http://localhost:8008 mindroom_coder
# ... etc
```

2. **Store credentials** in `matrix_state.yaml`:
```yaml
accounts:
  agent_calculator:
    username: mindroom_calculator
    password: "strong_password_here"
  agent_coder:
    username: mindroom_coder
    password: "another_strong_password"
```

3. **Start MindRoom** - it will use existing credentials instead of trying to register

## Security Best Practices

### 1. 🔑 Registration Control

**DO:**
- ✅ Keep registration disabled by default
- ✅ Use shared secrets or tokens for controlled registration
- ✅ Pre-create all necessary bot accounts
- ✅ Use strong, unique passwords for each bot

**DON'T:**
- ❌ Enable open registration on public servers
- ❌ Use weak or default passwords
- ❌ Share registration secrets in public repositories
- ❌ Leave registration enabled after creating bots

### 2. 🌐 Federation Security

Control which servers can federate with yours:

```yaml
# Synapse: homeserver.yaml
federation_domain_whitelist:
  - "trusted-partner.com"
  - "internal-server.local"

# Empty list = allow all (less secure)
# federation_domain_whitelist: []
```

### 3. 📊 Rate Limiting

Protect against abuse:

```yaml
# Synapse rate limits
rc_registration:
  per_second: 0.17  # ~10 per minute
  burst_count: 3

rc_login:
  address:
    per_second: 0.17
    burst_count: 3
  account:
    per_second: 0.17
    burst_count: 3
```

### 4. 🔐 Access Control

Limit who can create rooms and invite users:

```yaml
# Synapse
# Only registered users can create rooms
enable_room_list_search: false
allow_guest_access: false

# Require authentication for all endpoints
```

### 5. 📝 Monitoring

Monitor for security issues:

```bash
# Check registration attempts
docker logs myapp-synapse | grep -i "register"

# Monitor failed login attempts
docker logs myapp-synapse | grep -i "failed"

# Check for unusual activity
docker logs myapp-synapse | grep -i "error\|warning"
```

## Troubleshooting

### Bots Can't Register

1. **Check if registration is enabled** (it shouldn't be for security):
```bash
grep "enable_registration" instance_data/myapp/synapse/homeserver.yaml
```

2. **Use shared secret instead**:
```bash
cat instance_data/myapp/.matrix_registration_secret
```

3. **Verify bot credentials**:
```bash
cat matrix_state.yaml | grep -A2 "agent_"
```

### Registration Shared Secret Not Working

1. **Regenerate the secret**:
```bash
NEW_SECRET=$(openssl rand -hex 32)
sed -i "s/registration_shared_secret: .*/registration_shared_secret: \"$NEW_SECRET\"/" \
  instance_data/myapp/synapse/homeserver.yaml
echo "$NEW_SECRET" > instance_data/myapp/.matrix_registration_secret
docker restart myapp-synapse
```

### Accidental Open Registration

If registration was accidentally enabled:

1. **Immediately disable it**:
```bash
sed -i 's/enable_registration: true/enable_registration: false/' \
  instance_data/myapp/synapse/homeserver.yaml
docker restart myapp-synapse
```

2. **Check for unauthorized users**:
```bash
docker exec myapp-synapse \
  curl http://localhost:8008/_synapse/admin/v2/users \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

3. **Remove unauthorized accounts**:
```bash
docker exec myapp-synapse \
  curl -X DELETE http://localhost:8008/_synapse/admin/v1/deactivate/@spammer:myapp.com \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

## Advanced Security

### Using Registration Tokens

For maximum control, use registration tokens that expire:

```python
# Create time-limited token (expires in 1 hour)
import time
expiry_time = int(time.time() * 1000) + 3600000

curl -X POST http://localhost:8008/_synapse/admin/v1/registration_tokens/new \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -d '{
    "uses_allowed": 5,
    "expiry_time": '$expiry_time',
    "length": 16
  }'
```

### Implementing IP Allowlists

Restrict registration to specific IPs:

```yaml
# Synapse
ip_range_whitelist:
  - "127.0.0.1"
  - "10.0.0.0/8"
  - "172.16.0.0/12"
```

### Automated Bot Management

Create a script to manage bot lifecycle:

```bash
#!/bin/bash
# manage-bots.sh

case "$1" in
  create)
    # Create all bot accounts
    for agent in calculator coder writer; do
      ./register-bot.sh mindroom_$agent
    done
    ;;

  rotate-passwords)
    # Rotate all bot passwords
    for agent in calculator coder writer; do
      NEW_PASS=$(openssl rand -hex 16)
      # Update password via admin API
      # Update matrix_state.yaml
    done
    ;;

  audit)
    # List all users and check for unauthorized accounts
    docker exec myapp-synapse \
      curl http://localhost:8008/_synapse/admin/v2/users
    ;;
esac
```

## Summary

For maximum security:

1. **Never enable open registration** on production servers
2. **Use shared secrets** for bot registration only
3. **Pre-create bot accounts** before starting MindRoom
4. **Monitor logs** for suspicious activity
5. **Implement rate limiting** to prevent abuse
6. **Use strong passwords** and rotate them regularly
7. **Restrict federation** to trusted servers only
8. **Keep Matrix server updated** with security patches

Remember: Security is not a one-time setup but an ongoing process. Regularly review your configuration and monitor for unusual activity.
