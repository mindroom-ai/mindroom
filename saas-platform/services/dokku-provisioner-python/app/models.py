"""Pydantic models for request/response validation."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TierEnum(str, Enum):
    """Subscription tier options."""

    free = "free"
    starter = "starter"
    professional = "professional"
    enterprise = "enterprise"


class MatrixType(str, Enum):
    """Matrix server type options."""

    tuwunel = "tuwunel"
    synapse = "synapse"


class InstanceStatus(str, Enum):
    """Instance status options."""

    provisioning = "provisioning"
    running = "running"
    stopped = "stopped"
    failed = "failed"
    deprovisioning = "deprovisioning"
    deprovisioned = "deprovisioned"


class ResourceLimits(BaseModel):
    """Resource limits for an instance."""

    memory_mb: int = Field(default=512, ge=128, le=32768)
    cpu_limit: float = Field(default=0.5, ge=0.1, le=16.0)
    storage_gb: int = Field(default=1, ge=1, le=1000)
    agents: int = Field(default=1, ge=1, le=100)
    messages_per_day: int = Field(default=100, ge=10, le=100000)


class ProvisionRequest(BaseModel):
    """Request to provision a new instance."""

    subscription_id: str = Field(..., description="Unique subscription identifier")
    account_id: str = Field(..., description="Customer account identifier")
    tier: TierEnum = Field(..., description="Subscription tier")
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    config_overrides: dict[str, Any] | None = Field(
        None,
        description="Custom configuration overrides",
    )
    enable_matrix: bool = Field(
        default=False,
        description="Enable Matrix server",
    )
    matrix_type: MatrixType | None = Field(
        default=MatrixType.tuwunel,
        description="Type of Matrix server",
    )
    custom_domain: str | None = Field(
        None,
        description="Custom domain instead of default subdomain",
    )


class ProvisionResponse(BaseModel):
    """Response after provisioning an instance."""

    success: bool
    app_name: str = Field(..., description="Dokku app name")
    subdomain: str = Field(..., description="Instance subdomain")
    frontend_url: str = Field(..., description="Frontend URL")
    backend_url: str = Field(..., description="Backend API URL")
    matrix_url: str | None = Field(None, description="Matrix server URL")
    admin_password: str = Field(..., description="Admin password for the instance")
    provisioning_time_seconds: float = Field(..., description="Time taken to provision")
    message: str | None = Field(None, description="Additional information")


class DeprovisionRequest(BaseModel):
    """Request to deprovision an instance."""

    subscription_id: str = Field(..., description="Subscription to deprovision")
    app_name: str = Field(..., description="Dokku app name to remove")
    backup_data: bool = Field(
        default=True,
        description="Whether to backup data before deletion",
    )


class DeprovisionResponse(BaseModel):
    """Response after deprovisioning."""

    success: bool
    message: str
    backup_url: str | None = Field(None, description="URL to download backup")


class UpdateRequest(BaseModel):
    """Request to update an existing instance."""

    subscription_id: str = Field(..., description="Subscription to update")
    app_name: str = Field(..., description="Dokku app name")
    limits: ResourceLimits | None = Field(None, description="New resource limits")
    config_updates: dict[str, Any] | None = Field(
        None,
        description="Configuration updates",
    )
    tier: TierEnum | None = Field(None, description="New subscription tier")


class UpdateResponse(BaseModel):
    """Response after updating an instance."""

    success: bool
    message: str
    restart_required: bool = Field(
        default=False,
        description="Whether instance restart is required",
    )


class InstanceInfo(BaseModel):
    """Information about a provisioned instance."""

    app_name: str
    subscription_id: str
    account_id: str
    tier: TierEnum
    status: InstanceStatus
    subdomain: str
    frontend_url: str
    backend_url: str
    matrix_url: str | None = None
    limits: ResourceLimits
    created_at: datetime
    updated_at: datetime
    last_health_check: datetime | None = None


class HealthCheckResponse(BaseModel):
    """Health check response."""

    status: str = Field(default="healthy")
    dokku_connected: bool
    supabase_connected: bool
    version: str
    uptime_seconds: float
