# MindRoom Dokku Provisioner Service

This service provisions MindRoom instances on Dokku when customers subscribe to our SaaS platform.

## Overview

The Dokku Provisioner is a FastAPI service that:
- Provisions isolated MindRoom instances on Dokku
- Manages resource limits based on subscription tiers
- Configures agents and tools per tier
- Tracks instance status in Supabase
- Handles SSL certificates via Let's Encrypt
- Supports optional Matrix server deployment

## Architecture

```
Customer Subscribes → Supabase Webhook → Provisioner API → Dokku Server
                                                         ↓
                                                   Customer Instance
                                                   - Frontend (subdomain.mindroom.chat)
                                                   - Backend API (api.subdomain.mindroom.chat)
                                                   - PostgreSQL Database
                                                   - Redis Cache
                                                   - Optional Matrix Server
```

## Features

### Subscription Tiers

#### Free Tier
- 1 AI agent (Assistant)
- Basic tools (calculator, file)
- 256MB memory, 0.25 CPU
- 100 messages/day

#### Starter Tier
- 3 AI agents (Assistant, Researcher, Coder)
- Extended tools (web search, shell, GitHub)
- 512MB memory, 0.5 CPU
- 500 messages/day

#### Professional Tier
- 6 AI agents with Claude Sonnet
- All tools unlocked
- 2GB memory, 1.0 CPU
- 2000 messages/day

#### Enterprise Tier
- All agents including Orchestrator
- Claude Opus & GPT-4
- 8GB memory, 4.0 CPU
- Unlimited messages
- Custom domains

## Setup

### Prerequisites

1. **Dokku Server**: A Dokku installation with these plugins:
   - postgres
   - redis
   - letsencrypt
   - resource-limits
   - storage

2. **SSH Access**: SSH key pair for Dokku server access

3. **Supabase**: Project with instances table

4. **Docker Images**: Pre-built MindRoom frontend and backend images

### Installation

1. Clone the repository:
```bash
cd services/dokku-provisioner
```

2. Set up SSH key:
```bash
mkdir -p ssh
cp /path/to/dokku_private_key ssh/dokku_key
chmod 600 ssh/dokku_key
```

3. Configure environment:
```bash
cp .env.example .env
# Edit .env with your settings
```

4. Add Dokku server's SSH key to authorized_keys:
```bash
# On your Dokku server:
cat ~/.ssh/id_rsa.pub | ssh root@dokku-server "sudo sshcommand acl-add dokku provisioner"
```

### Running the Service

#### Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run with auto-reload
uvicorn app.main:app --reload --port 8002
```

#### Production with Docker
```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f dokku-provisioner

# Check health
curl http://localhost:8002/health
```

## API Endpoints

### Health Checks

```bash
GET /health
GET /ready
GET /live
```

### Provisioning

#### Provision Instance
```bash
POST /api/v1/provision
{
  "subscription_id": "sub_123",
  "account_id": "acc_456",
  "tier": "professional",
  "limits": {
    "memory_mb": 2048,
    "cpu_limit": 1.0,
    "storage_gb": 10,
    "agents": 6,
    "messages_per_day": 2000
  },
  "enable_matrix": true,
  "matrix_type": "tuwunel"
}
```

#### Deprovision Instance
```bash
DELETE /api/v1/deprovision
{
  "subscription_id": "sub_123",
  "app_name": "mr-sub123-abc12345",
  "backup_data": true
}
```

#### Update Instance
```bash
PUT /api/v1/update
{
  "subscription_id": "sub_123",
  "app_name": "mr-sub123-abc12345",
  "tier": "enterprise",
  "limits": {
    "memory_mb": 8192,
    "cpu_limit": 4.0
  }
}
```

#### Get Instance Status
```bash
GET /api/v1/status/{subscription_id}
```

## Dokku Configuration

### Required Dokku Plugins

Install on your Dokku server:
```bash
# PostgreSQL plugin
sudo dokku plugin:install https://github.com/dokku/dokku-postgres.git postgres

# Redis plugin
sudo dokku plugin:install https://github.com/dokku/dokku-redis.git redis

# Let's Encrypt plugin
sudo dokku plugin:install https://github.com/dokku/dokku-letsencrypt.git

# Resource limits plugin
sudo dokku plugin:install https://github.com/dokku/dokku-resource-limit.git resource-limit

# Storage plugin (built-in, just enable)
dokku storage:ensure-directory /var/lib/dokku/data/storage
```

### Dokku App Naming Convention

Apps are named: `mr-{subscription_prefix}-{random}`
- Main app: `mr-sub123-abc12345`
- Backend: `mr-sub123-abc12345-backend`
- Frontend: `mr-sub123-abc12345-frontend`
- Matrix: `mr-sub123-abc12345-matrix` (optional)

## Monitoring

### Logs
```bash
# Service logs
docker-compose logs -f dokku-provisioner

# Instance logs on Dokku
dokku logs mr-sub123-abc12345-backend
dokku logs mr-sub123-abc12345-frontend
```

### Metrics
- Instance provisioning time
- Resource usage per tier
- Failed provisioning attempts
- Active instances count

## Security

1. **SSH Key Security**:
   - Store SSH keys securely
   - Use read-only volume mounts
   - Rotate keys regularly

2. **API Authentication**:
   - Implement API key authentication
   - Use webhook signatures from Supabase

3. **Resource Isolation**:
   - Each instance has isolated containers
   - Resource limits enforced by Dokku
   - Separate databases per instance

4. **Network Security**:
   - SSL certificates via Let's Encrypt
   - Firewall rules on Dokku server
   - Private network for inter-service communication

## Troubleshooting

### Common Issues

1. **SSH Connection Failed**
   - Check SSH key permissions (600)
   - Verify Dokku user has proper access
   - Test connection: `ssh dokku@server version`

2. **Provisioning Timeout**
   - Check Dokku server resources
   - Verify Docker images are available
   - Review Dokku plugin installations

3. **SSL Certificate Failed**
   - Ensure domain DNS points to Dokku server
   - Check Let's Encrypt rate limits
   - Verify port 80/443 are open

### Debug Mode
```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
docker-compose restart dokku-provisioner
```

## Development

### Running Tests
```bash
pytest tests/
```

### Adding New Tiers
Edit `app/services/config_generator.py` to:
1. Add tier configuration
2. Define agents for the tier
3. Set resource limits
4. Configure available tools

### Extending Dokku Commands
Add new methods to `app/dokku/client.py`:
```python
def custom_command(self, app_name: str, params: str) -> bool:
    status, _, _ = self.execute(f"custom:command {app_name} {params}")
    return status == 0
```

## License

Proprietary - MindRoom SaaS Platform

## Support

For issues or questions, contact the MindRoom platform team.
