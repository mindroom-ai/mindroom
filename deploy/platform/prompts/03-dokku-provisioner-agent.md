# Agent 3: Dokku Provisioner Service

## Project Context

You are working on MindRoom, an AI agent platform that provides isolated AI assistants to customers via a SaaS model.

### Understanding the Current System

First, read these critical files to understand what you're provisioning:
1. `README.md` - Product overview
2. `deploy/deploy.py` - Current deployment mechanism (YOU WILL ADAPT THIS)
3. `deploy/docker-compose.yml` - Container configuration
4. `config.yaml` - MindRoom agent configurations
5. `deploy/docker-compose.platform.yml` - Platform services we're building

### The Goal

Build a Python FastAPI service that provisions MindRoom instances on Dokku when customers subscribe. Each customer gets:
- Isolated Docker containers (frontend + backend)
- Their own subdomain (e.g., customer-abc.mindroom.chat)
- PostgreSQL database (for their agent memory)
- Resource limits based on subscription tier
- Optional Matrix server for federation

## Your Specific Task

You will work ONLY in the `services/dokku-provisioner/` directory to build the provisioning service.

### Step 1: Initialize Project

```bash
cd services/dokku-provisioner
# Create Python project structure
```

Create `requirements.txt`:
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
paramiko==3.3.1
asyncpg==0.29.0
httpx==0.25.2
python-dotenv==1.0.0
jinja2==3.1.2
pyyaml==6.0.1
```

### Step 2: Project Structure

```
services/dokku-provisioner/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app
│   ├── config.py                  # Configuration
│   ├── models.py                  # Pydantic models
│   ├── dokku/
│   │   ├── __init__.py
│   │   ├── client.py             # Dokku SSH client
│   │   ├── commands.py           # Dokku command builders
│   │   └── templates.py          # Command templates
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── provision.py          # Provisioning endpoints
│   │   └── health.py             # Health checks
│   └── services/
│       ├── __init__.py
│       ├── supabase.py           # Update instance status
│       └── config_generator.py    # Generate MindRoom configs
├── templates/
│   ├── dokku_commands.sh.j2       # Dokku command templates
│   ├── config.yaml.j2             # MindRoom config template
│   └── env_vars.sh.j2             # Environment variables
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

### Step 3: Core Implementation

#### A. `app/main.py` - FastAPI Application
```python
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from . import config
from .routers import provision, health
from .models import ProvisionRequest, DeprovisionRequest, UpdateRequest

app = FastAPI(title="MindRoom Dokku Provisioner")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(provision.router, prefix="/api/v1", tags=["provisioning"])

@app.on_event("startup")
async def startup():
    """Initialize SSH connection pool and test Dokku access"""
    # Test Dokku connection
    from .dokku.client import test_connection
    if not test_connection():
        raise Exception("Cannot connect to Dokku server")
```

#### B. `app/config.py` - Configuration
```python
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Server
    port: int = 8002

    # Dokku SSH
    dokku_host: str
    dokku_user: str = "dokku"
    dokku_ssh_key_path: str = "/app/ssh/dokku_key"
    dokku_port: int = 22

    # Domains
    base_domain: str = "mindroom.chat"

    # Docker Images
    mindroom_backend_image: str = "mindroom/backend:latest"
    mindroom_frontend_image: str = "mindroom/frontend:latest"

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Resource Limits (defaults)
    default_memory_limit: str = "512m"
    default_cpu_limit: str = "0.5"

    # Paths
    instance_data_base: str = "/var/lib/dokku/data/storage"

    class Config:
        env_file = ".env"

settings = Settings()
```

#### C. `app/models.py` - Request/Response Models
```python
from pydantic import BaseModel
from typing import Optional, Dict, Any
from enum import Enum

class TierEnum(str, Enum):
    free = "free"
    starter = "starter"
    professional = "professional"
    enterprise = "enterprise"

class ResourceLimits(BaseModel):
    memory_mb: int = 512
    cpu_limit: float = 0.5
    storage_gb: int = 1
    agents: int = 1
    messages_per_day: int = 100

class ProvisionRequest(BaseModel):
    subscription_id: str
    account_id: str
    tier: TierEnum
    limits: ResourceLimits
    config_overrides: Optional[Dict[str, Any]] = None
    enable_matrix: bool = False
    matrix_type: Optional[str] = "tuwunel"  # or "synapse"

class ProvisionResponse(BaseModel):
    success: bool
    app_name: str
    subdomain: str
    frontend_url: str
    backend_url: str
    matrix_url: Optional[str] = None
    admin_password: str
    provisioning_time_seconds: float

class DeprovisionRequest(BaseModel):
    app_name: str
    backup_data: bool = True

class UpdateRequest(BaseModel):
    app_name: str
    limits: Optional[ResourceLimits] = None
    config_updates: Optional[Dict[str, Any]] = None
```

#### D. `app/dokku/client.py` - Dokku SSH Client
```python
import paramiko
from typing import List, Optional, Tuple
import json
from ..config import settings
import logging

logger = logging.getLogger(__name__)

class DokkuClient:
    """SSH client for executing Dokku commands"""

    def __init__(self):
        self.ssh = None
        self.connect()

    def connect(self):
        """Establish SSH connection to Dokku server"""
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Use SSH key
        key = paramiko.RSAKey.from_private_key_file(settings.dokku_ssh_key_path)

        self.ssh.connect(
            hostname=settings.dokku_host,
            port=settings.dokku_port,
            username=settings.dokku_user,
            pkey=key,
            timeout=30
        )
        logger.info(f"Connected to Dokku at {settings.dokku_host}")

    def execute(self, command: str) -> Tuple[int, str, str]:
        """Execute a Dokku command via SSH"""
        if not self.ssh:
            self.connect()

        # Ensure command starts with 'dokku' for safety
        if not command.startswith("dokku"):
            command = f"dokku {command}"

        logger.debug(f"Executing: {command}")
        stdin, stdout, stderr = self.ssh.exec_command(command)

        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        error = stderr.read().decode()

        if exit_status != 0:
            logger.error(f"Command failed: {command}\nError: {error}")

        return exit_status, output, error

    def app_exists(self, app_name: str) -> bool:
        """Check if a Dokku app exists"""
        status, output, _ = self.execute(f"apps:exists {app_name}")
        return status == 0

    def create_app(self, app_name: str) -> bool:
        """Create a new Dokku app"""
        if self.app_exists(app_name):
            logger.warning(f"App {app_name} already exists")
            return False

        status, _, _ = self.execute(f"apps:create {app_name}")
        return status == 0

    def destroy_app(self, app_name: str, force: bool = True) -> bool:
        """Destroy a Dokku app"""
        force_flag = "--force" if force else ""
        status, _, _ = self.execute(f"apps:destroy {app_name} {force_flag}")
        return status == 0

    def set_config(self, app_name: str, config: dict) -> bool:
        """Set environment variables for an app"""
        # Build config string
        config_str = " ".join([f"{k}={v}" for k, v in config.items()])
        status, _, _ = self.execute(f"config:set {app_name} {config_str}")
        return status == 0

    def create_postgres(self, service_name: str) -> bool:
        """Create a PostgreSQL database"""
        status, _, _ = self.execute(f"postgres:create {service_name}")
        return status == 0

    def link_postgres(self, service_name: str, app_name: str) -> bool:
        """Link PostgreSQL to an app"""
        status, _, _ = self.execute(f"postgres:link {service_name} {app_name}")
        return status == 0

    def create_redis(self, service_name: str) -> bool:
        """Create a Redis instance"""
        status, _, _ = self.execute(f"redis:create {service_name}")
        return status == 0

    def link_redis(self, service_name: str, app_name: str) -> bool:
        """Link Redis to an app"""
        status, _, _ = self.execute(f"redis:link {service_name} {app_name}")
        return status == 0

    def set_domains(self, app_name: str, domains: List[str]) -> bool:
        """Set domains for an app"""
        for domain in domains:
            status, _, _ = self.execute(f"domains:add {app_name} {domain}")
            if status != 0:
                return False
        return True

    def enable_letsencrypt(self, app_name: str) -> bool:
        """Enable Let's Encrypt SSL for an app"""
        status, _, _ = self.execute(f"letsencrypt:enable {app_name}")
        return status == 0

    def set_resource_limits(self, app_name: str, memory: str, cpu: str) -> bool:
        """Set resource limits for an app"""
        # Dokku resource limits plugin
        status1, _, _ = self.execute(f"resource:limit {app_name} --memory {memory}")
        status2, _, _ = self.execute(f"resource:limit {app_name} --cpu {cpu}")
        return status1 == 0 and status2 == 0

    def deploy_image(self, app_name: str, image: str) -> bool:
        """Deploy a Docker image to an app"""
        status, _, _ = self.execute(f"git:from-image {app_name} {image}")
        return status == 0

    def create_storage(self, app_name: str, mount_path: str) -> bool:
        """Create persistent storage for an app"""
        storage_path = f"{settings.instance_data_base}/{app_name}"
        status, _, _ = self.execute(
            f"storage:mount {app_name} {storage_path}:{mount_path}"
        )
        return status == 0

# Singleton instance
dokku_client = DokkuClient()

def test_connection() -> bool:
    """Test Dokku connection"""
    try:
        status, output, _ = dokku_client.execute("version")
        return status == 0
    except Exception as e:
        logger.error(f"Dokku connection test failed: {e}")
        return False
```

#### E. `app/routers/provision.py` - Provisioning Endpoints
```python
from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Optional
import secrets
import string
import time
from ..models import (
    ProvisionRequest, ProvisionResponse,
    DeprovisionRequest, UpdateRequest
)
from ..dokku.client import dokku_client
from ..services.config_generator import generate_mindroom_config
from ..services.supabase import update_instance_status

router = APIRouter()

def generate_app_name(subscription_id: str) -> str:
    """Generate unique app name for Dokku"""
    # Dokku app names must be lowercase alphanumeric
    suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return f"mindroom-{suffix}"

def generate_password() -> str:
    """Generate secure password for admin user"""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(20))

@router.post("/provision", response_model=ProvisionResponse)
async def provision_instance(
    request: ProvisionRequest,
    background_tasks: BackgroundTasks
):
    """Provision a new MindRoom instance on Dokku"""

    start_time = time.time()
    app_name = generate_app_name(request.subscription_id)
    subdomain = f"{app_name}.{settings.base_domain}"
    admin_password = generate_password()

    try:
        # 1. Create Dokku app
        if not dokku_client.create_app(app_name):
            raise HTTPException(500, "Failed to create Dokku app")

        # 2. Create and link PostgreSQL
        db_name = f"{app_name}-db"
        if not dokku_client.create_postgres(db_name):
            raise HTTPException(500, "Failed to create PostgreSQL")
        if not dokku_client.link_postgres(db_name, app_name):
            raise HTTPException(500, "Failed to link PostgreSQL")

        # 3. Create and link Redis (for caching)
        redis_name = f"{app_name}-redis"
        if not dokku_client.create_redis(redis_name):
            raise HTTPException(500, "Failed to create Redis")
        if not dokku_client.link_redis(redis_name, app_name):
            raise HTTPException(500, "Failed to link Redis")

        # 4. Set environment variables
        env_vars = {
            "INSTANCE_ID": request.subscription_id,
            "ACCOUNT_ID": request.account_id,
            "TIER": request.tier,
            "ADMIN_PASSWORD": admin_password,
            "BASE_URL": f"https://{subdomain}",
            "MATRIX_HOMESERVER": "https://matrix.org",  # Default, can be customized
            "LOG_LEVEL": "INFO",
            "MAX_AGENTS": str(request.limits.agents),
            "MAX_MESSAGES_PER_DAY": str(request.limits.messages_per_day),
        }

        if not dokku_client.set_config(app_name, env_vars):
            raise HTTPException(500, "Failed to set environment variables")

        # 5. Set resource limits
        memory = f"{request.limits.memory_mb}m"
        cpu = str(request.limits.cpu_limit)
        if not dokku_client.set_resource_limits(app_name, memory, cpu):
            raise HTTPException(500, "Failed to set resource limits")

        # 6. Create persistent storage
        if not dokku_client.create_storage(app_name, "/app/data"):
            raise HTTPException(500, "Failed to create storage")

        # 7. Generate and deploy MindRoom config
        config = generate_mindroom_config(request.tier, request.config_overrides)
        # This would need to be deployed to the storage volume

        # 8. Deploy the MindRoom Docker images
        # Deploy backend
        backend_app = f"{app_name}-backend"
        if not dokku_client.create_app(backend_app):
            raise HTTPException(500, "Failed to create backend app")
        if not dokku_client.deploy_image(backend_app, settings.mindroom_backend_image):
            raise HTTPException(500, "Failed to deploy backend")

        # Deploy frontend
        frontend_app = f"{app_name}-frontend"
        if not dokku_client.create_app(frontend_app):
            raise HTTPException(500, "Failed to create frontend app")
        if not dokku_client.deploy_image(frontend_app, settings.mindroom_frontend_image):
            raise HTTPException(500, "Failed to deploy frontend")

        # 9. Set up domains
        domains = [
            subdomain,  # Main domain
            f"api.{subdomain}",  # API subdomain
        ]
        if not dokku_client.set_domains(app_name, domains):
            raise HTTPException(500, "Failed to set domains")

        # 10. Enable SSL with Let's Encrypt
        if not dokku_client.enable_letsencrypt(app_name):
            # Non-fatal, can be retried later
            print(f"Warning: Failed to enable SSL for {app_name}")

        # 11. Optional: Set up Matrix server
        matrix_url = None
        if request.enable_matrix:
            # Deploy Matrix server (simplified)
            matrix_app = f"{app_name}-matrix"
            if dokku_client.create_app(matrix_app):
                # Deploy Tuwunel or Synapse based on request
                matrix_image = f"matrixdotorg/{request.matrix_type}:latest"
                dokku_client.deploy_image(matrix_app, matrix_image)
                matrix_url = f"https://matrix.{subdomain}"

        # 12. Update Supabase with instance details
        background_tasks.add_task(
            update_instance_status,
            subscription_id=request.subscription_id,
            status="running",
            urls={
                "frontend": f"https://{subdomain}",
                "backend": f"https://api.{subdomain}",
                "matrix": matrix_url,
            }
        )

        provisioning_time = time.time() - start_time

        return ProvisionResponse(
            success=True,
            app_name=app_name,
            subdomain=subdomain,
            frontend_url=f"https://{subdomain}",
            backend_url=f"https://api.{subdomain}",
            matrix_url=matrix_url,
            admin_password=admin_password,
            provisioning_time_seconds=provisioning_time
        )

    except Exception as e:
        # Cleanup on failure
        dokku_client.destroy_app(app_name, force=True)
        dokku_client.destroy_app(f"{app_name}-backend", force=True)
        dokku_client.destroy_app(f"{app_name}-frontend", force=True)

        # Update status in Supabase
        background_tasks.add_task(
            update_instance_status,
            subscription_id=request.subscription_id,
            status="failed",
            error=str(e)
        )

        raise HTTPException(500, f"Provisioning failed: {str(e)}")

@router.delete("/deprovision")
async def deprovision_instance(request: DeprovisionRequest):
    """Remove a MindRoom instance from Dokku"""

    try:
        # Backup data if requested
        if request.backup_data:
            # Implement backup logic
            pass

        # Destroy all related apps
        dokku_client.destroy_app(request.app_name, force=True)
        dokku_client.destroy_app(f"{request.app_name}-backend", force=True)
        dokku_client.destroy_app(f"{request.app_name}-frontend", force=True)
        dokku_client.destroy_app(f"{request.app_name}-matrix", force=True)

        # Destroy database and Redis
        dokku_client.execute(f"postgres:destroy {request.app_name}-db --force")
        dokku_client.execute(f"redis:destroy {request.app_name}-redis --force")

        return {"success": True, "message": f"Instance {request.app_name} deprovisioned"}

    except Exception as e:
        raise HTTPException(500, f"Deprovisioning failed: {str(e)}")

@router.put("/update")
async def update_instance(request: UpdateRequest):
    """Update an existing instance (limits, config, etc.)"""

    try:
        # Update resource limits if provided
        if request.limits:
            memory = f"{request.limits.memory_mb}m"
            cpu = str(request.limits.cpu_limit)
            dokku_client.set_resource_limits(request.app_name, memory, cpu)

        # Update config if provided
        if request.config_updates:
            dokku_client.set_config(request.app_name, request.config_updates)

        # Restart app to apply changes
        dokku_client.execute(f"ps:restart {request.app_name}")

        return {"success": True, "message": f"Instance {request.app_name} updated"}

    except Exception as e:
        raise HTTPException(500, f"Update failed: {str(e)}")

@router.get("/status/{app_name}")
async def get_instance_status(app_name: str):
    """Check the status of an instance"""

    try:
        # Check if app exists
        if not dokku_client.app_exists(app_name):
            raise HTTPException(404, f"Instance {app_name} not found")

        # Get app info
        status, output, _ = dokku_client.execute(f"apps:report {app_name}")

        # Parse output and return status
        return {
            "app_name": app_name,
            "exists": True,
            "status": "running" if status == 0 else "unknown",
            "details": output
        }

    except Exception as e:
        raise HTTPException(500, f"Status check failed: {str(e)}")
```

#### F. `app/services/config_generator.py` - MindRoom Config Generator
```python
import yaml
from typing import Dict, Any, Optional

def generate_mindroom_config(tier: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Generate MindRoom config.yaml based on subscription tier"""

    # Base configuration for all tiers
    base_config = {
        "models": {
            "available_models": {
                "haiku": {
                    "model_id": "claude-3-haiku-20240307",
                    "display_name": "Claude 3 Haiku",
                    "provider": "anthropic"
                }
            }
        },
        "memory": {
            "provider": "mem0"
        }
    }

    # Tier-specific agents and features
    if tier == "free":
        agents = {
            "assistant": {
                "display_name": "Assistant",
                "role": "General AI assistant",
                "model": "haiku",
                "tools": ["calculator", "file"]
            }
        }
        rooms = ["lobby"]

    elif tier == "starter":
        agents = {
            "assistant": {
                "display_name": "Assistant",
                "role": "General AI assistant",
                "model": "haiku",
                "tools": ["calculator", "file", "shell", "web_search"]
            },
            "researcher": {
                "display_name": "Researcher",
                "role": "Research and analysis",
                "model": "haiku",
                "tools": ["web_search", "wikipedia", "arxiv"]
            },
            "coder": {
                "display_name": "Coder",
                "role": "Programming assistant",
                "model": "haiku",
                "tools": ["file", "shell", "github"]
            }
        }
        rooms = ["lobby", "research", "development"]

    elif tier == "professional":
        # Full agent set with Sonnet model
        base_config["models"]["available_models"]["sonnet"] = {
            "model_id": "claude-3-5-sonnet-20241022",
            "display_name": "Claude 3.5 Sonnet",
            "provider": "anthropic"
        }

        agents = {
            # Include all agents from config.yaml
            # This would be loaded from a template
        }
        rooms = ["lobby", "research", "development", "team", "automation"]

    else:  # enterprise
        # Everything unlocked
        agents = {}  # Load full config
        rooms = []  # All rooms

    config = {
        **base_config,
        "agents": agents,
        "rooms": {room: {"description": f"{room} room"} for room in rooms}
    }

    # Apply any overrides
    if overrides:
        # Deep merge overrides into config
        config.update(overrides)

    return config

def save_config_to_storage(config: Dict[str, Any], storage_path: str):
    """Save config to instance storage"""
    config_path = f"{storage_path}/config.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
```

### Step 4: Templates

Create `templates/dokku_commands.sh.j2`:
```bash
#!/bin/bash
# Dokku provisioning commands for {{ app_name }}

# Create app
dokku apps:create {{ app_name }}

# Create and link PostgreSQL
dokku postgres:create {{ app_name }}-db
dokku postgres:link {{ app_name }}-db {{ app_name }}

# Create and link Redis
dokku redis:create {{ app_name }}-redis
dokku redis:link {{ app_name }}-redis {{ app_name }}

# Set config
{% for key, value in env_vars.items() %}
dokku config:set {{ app_name }} {{ key }}="{{ value }}"
{% endfor %}

# Set resource limits
dokku resource:limit {{ app_name }} --memory {{ memory_limit }}
dokku resource:limit {{ app_name }} --cpu {{ cpu_limit }}

# Set domains
{% for domain in domains %}
dokku domains:add {{ app_name }} {{ domain }}
{% endfor %}

# Enable SSL
dokku letsencrypt:enable {{ app_name }}

# Deploy image
dokku git:from-image {{ app_name }} {{ image }}
```

### Step 5: Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install SSH client
RUN apt-get update && apt-get install -y \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/
COPY templates/ ./templates/

# Create SSH directory
RUN mkdir -p /app/ssh

# Run as non-root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
```

### Step 6: Environment Variables

Create `.env.example`:
```bash
# Dokku SSH Configuration
DOKKU_HOST=your-dokku-server.com
DOKKU_USER=dokku
DOKKU_SSH_KEY_PATH=/app/ssh/dokku_key
DOKKU_PORT=22

# Domain Configuration
BASE_DOMAIN=mindroom.chat

# Docker Images
MINDROOM_BACKEND_IMAGE=mindroom/backend:latest
MINDROOM_FRONTEND_IMAGE=mindroom/frontend:latest

# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...

# Resource Defaults
DEFAULT_MEMORY_LIMIT=512m
DEFAULT_CPU_LIMIT=0.5

# Storage
INSTANCE_DATA_BASE=/var/lib/dokku/data/storage
```

## Important Implementation Notes

1. **SSH Key Setup**: You need to add the provisioner's public SSH key to the Dokku server's authorized_keys
2. **Dokku Plugins Required**:
   - postgres
   - redis
   - letsencrypt
   - resource-limits
   - storage
3. **Security**: Never expose the Dokku SSH key. Use secrets management.
4. **Idempotency**: Make operations idempotent - check if resources exist before creating
5. **Cleanup**: Always cleanup resources if provisioning fails
6. **Monitoring**: Add health checks for each deployed instance

## Output Files Required

```
services/dokku-provisioner/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── models.py
│   ├── dokku/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── commands.py
│   │   └── templates.py
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── provision.py
│   │   └── health.py
│   └── services/
│       ├── __init__.py
│       ├── supabase.py
│       └── config_generator.py
├── templates/
│   ├── dokku_commands.sh.j2
│   ├── config.yaml.j2
│   └── env_vars.sh.j2
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## Critical Notes

1. DO NOT modify any files outside `services/dokku-provisioner/`
2. DO NOT touch the existing deploy.py - this is a replacement
3. This service is critical - instances won't provision if this fails
4. Log everything for debugging
5. Implement retry logic for Dokku commands
6. Consider rollback mechanisms for failed provisioning

Remember: This service directly controls customer infrastructure. Make it robust, well-logged, and failsafe.
