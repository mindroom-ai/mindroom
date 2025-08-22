# 🔐 Production Authentication with Authelia

## Overview

Authelia is a production-ready, actively maintained authentication server that provides:
- 🔑 Single Sign-On (SSO)
- 🔐 Two-Factor Authentication (2FA)
- 🛡️ Brute force protection
- 📧 Password reset via email
- 👥 User groups and access control
- 🔄 Active development and security updates

## Quick Start

### 1. Create an instance
```bash
./deploy.py create myapp --domain myapp.com
```

### 2. Setup Authelia
```bash
./deploy/setup-authelia.sh myapp
```

### 3. Start with authentication
```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.authelia.yml -p myapp up -d
```

### 4. Access your app
- App: `https://myapp.com` (will redirect to auth)
- Auth portal: `https://auth.myapp.com`

## User Management

### Add a new user

1. Generate password hash:
```bash
docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password 'SecurePassword123!'
```

2. Edit `instance_data/myapp/authelia/users_database.yml`:
```yaml
users:
  john:
    disabled: false
    displayname: "John Doe"
    password: "$argon2id$v=19$m=65536,t=3,p=4$..."  # paste hash here
    email: john@example.com
    groups:
      - users
```

3. Restart Authelia:
```bash
docker compose -p myapp restart authelia
```

## Configuration

### Enable 2FA (Recommended for production)

Edit `instance_data/myapp/authelia/configuration.yml`:
```yaml
access_control:
  rules:
    - domain: '*.myapp.com'
      policy: two_factor  # Changed from one_factor
```

### Setup Email Notifications

For password resets and 2FA setup, configure SMTP:

```yaml
notifier:
  smtp:
    host: smtp.gmail.com
    port: 587
    username: your-email@gmail.com
    password: your-app-specific-password  # Not your regular password!
    sender: "MindRoom Auth <your-email@gmail.com>"
```

### Custom Access Rules

Control who can access what:

```yaml
access_control:
  rules:
    # Public access
    - domain: public.myapp.com
      policy: bypass

    # Admin only
    - domain: admin.myapp.com
      policy: two_factor
      subject:
        - group:admins

    # All authenticated users
    - domain: '*.myapp.com'
      policy: one_factor
```

## Security Best Practices

### 1. 🔑 Strong Passwords
- Minimum 12 characters
- Use a password manager
- Unique passwords per user

### 2. 🔐 Enable 2FA
```yaml
# In configuration.yml
access_control:
  default_policy: deny
  rules:
    - domain: '*.myapp.com'
      policy: two_factor
```

### 3. 🔄 Regular Updates
```bash
# Pull latest Authelia image
docker pull authelia/authelia:latest
docker compose -p myapp up -d
```

### 4. 🛡️ Secure Secrets
```bash
# Generate strong secrets
openssl rand -hex 32  # For JWT secret
openssl rand -hex 32  # For session secret
```

### 5. 📊 Monitor Logs
```bash
# View auth attempts
docker logs myapp-authelia

# Follow logs
docker logs -f myapp-authelia
```

## Troubleshooting

### Reset a user's password
```bash
# Generate new hash
docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password 'NewPassword123!'

# Update users_database.yml with new hash
# Restart Authelia
```

### User locked out (brute force protection)
```bash
# Check regulation database
docker exec -it myapp-authelia sh
sqlite3 /config/db.sqlite3
DELETE FROM authentication_logs WHERE username='john';
.exit
```

### Session issues
```bash
# Clear Redis sessions
docker exec -it myapp-authelia-redis redis-cli FLUSHALL
```

## Comparison with our custom auth

| Feature | Our Custom Auth | Authelia |
|---------|----------------|----------|
| Session Management | ❌ In-memory | ✅ Redis (persistent) |
| 2FA Support | ❌ None | ✅ TOTP/WebAuthn |
| Password Reset | ❌ None | ✅ Email/SMS |
| Brute Force Protection | ❌ None | ✅ Built-in |
| CSRF Protection | ❌ None | ✅ Built-in |
| Security Audits | ❌ None | ✅ Regular |
| Active Development | ❌ No | ✅ Very active |
| Production Ready | ❌ No | ✅ Yes |

## Migration from Custom Auth

Since we've already reverted the custom auth:

1. ✅ Code is clean (no auth code in app)
2. ✅ Authentication happens at proxy level
3. ✅ No app changes needed
4. ✅ Professional, secure solution

## Resources

- [Authelia Documentation](https://www.authelia.com/docs/)
- [Configuration Reference](https://www.authelia.com/configuration/prologue/introduction/)
- [Security Features](https://www.authelia.com/overview/security/introduction/)
- [Integration Guide](https://www.authelia.com/integration/proxies/traefik/)

## Support

- GitHub: https://github.com/authelia/authelia
- Discord: https://discord.authelia.com
- Security: security@authelia.com
