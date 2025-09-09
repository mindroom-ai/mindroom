from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InstanceOut(BaseModel):
    id: str
    instance_id: int | str
    subscription_id: str
    subdomain: str | None = None
    status: Literal["provisioning", "running", "stopped", "failed", "error"]
    frontend_url: str | None = None
    backend_url: str | None = None
    matrix_server_url: str | None = None
    tier: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class InstancesResponse(BaseModel):
    instances: list[InstanceOut]


class SubscriptionOut(BaseModel):
    id: str
    account_id: str
    tier: Literal["free", "starter", "professional", "enterprise"]
    status: Literal["active", "cancelled", "past_due", "trialing", "incomplete"]
    stripe_subscription_id: str | None = None
    stripe_customer_id: str | None = None
    current_period_end: str | None = None
    max_agents: int
    max_messages_per_day: int
    max_storage_gb: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ActionResult(BaseModel):
    success: bool
    message: str


class ProvisionResponse(BaseModel):
    success: bool
    message: str
    customer_id: int | str
    frontend_url: str | None = None
    api_url: str | None = None
    matrix_url: str | None = None


class UsageMetricOut(BaseModel):
    id: str | None = None
    subscription_id: str
    metric_date: str
    messages_sent: int
    agents_used: int
    storage_used_gb: float | int
    created_at: str | None = None


class UsageAggregateOut(BaseModel):
    total_messages: int = Field(alias="totalMessages")
    total_agents: int = Field(alias="totalAgents")
    total_storage: float | int = Field(alias="totalStorage")


class UsageResponse(BaseModel):
    usage: list[UsageMetricOut]
    aggregated: UsageAggregateOut


class UrlResponse(BaseModel):
    url: str


class AdminStatusOut(BaseModel):
    is_admin: bool


class StatusResponse(BaseModel):
    status: str


class HealthResponse(BaseModel):
    status: str
    supabase: bool
    stripe: bool


class SyncUpdateOut(BaseModel):
    instance_id: int | str
    old_status: str
    new_status: str
    reason: str


class SyncResult(BaseModel):
    total: int
    synced: int
    errors: int
    updates: list[SyncUpdateOut]


class AdminStatsOut(BaseModel):
    accounts: int
    active_subscriptions: int
    running_instances: int


class UpdateAccountStatusResponse(BaseModel):
    status: str
    account_id: str
    new_status: str
