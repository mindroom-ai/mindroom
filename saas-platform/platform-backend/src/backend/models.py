"""Data models for the platform backend."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class InstanceOut(BaseModel):
    """Instance information output model."""

    id: str
    instance_id: int | str
    subscription_id: str
    subdomain: str | None = None
    status: Literal[
        "provisioning",
        "running",
        "stopped",
        "failed",
        "error",
        "deprovisioned",
        "restarting",
    ]
    frontend_url: str | None = None
    backend_url: str | None = None
    matrix_server_url: str | None = None
    tier: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    kubernetes_synced_at: str | None = None
    status_hint: str | None = None


class InstancesResponse(BaseModel):
    """Response model for listing multiple instances."""

    instances: list[InstanceOut]


class SubscriptionOut(BaseModel):
    """Subscription information output model."""

    id: str
    account_id: str
    tier: Literal["free", "starter", "professional", "enterprise"]
    status: Literal["active", "cancelled", "past_due", "trialing", "incomplete"]
    stripe_subscription_id: str | None = None
    stripe_customer_id: str | None = None
    current_period_start: str | None = None
    current_period_end: str | None = None
    trial_ends_at: str | None = None
    cancelled_at: str | None = None
    max_agents: int
    max_messages_per_day: int
    max_storage_gb: int
    created_at: str | None = None
    updated_at: str | None = None


class ActionResult(BaseModel):
    """Result model for action operations."""

    success: bool
    message: str


class ProvisionResponse(BaseModel):
    """Response model for provisioning operations."""

    success: bool
    message: str
    customer_id: int | str
    frontend_url: str | None = None
    api_url: str | None = None
    matrix_url: str | None = None


class UsageMetricOut(BaseModel):
    """Usage metric output model."""

    id: str | None = None
    subscription_id: str
    metric_date: str
    messages_sent: int
    agents_used: int
    storage_used_gb: float | int
    created_at: str | None = None


class UsageAggregateOut(BaseModel):
    """Aggregated usage statistics model."""

    model_config = {"populate_by_name": True}

    total_messages: int = Field(alias="totalMessages")
    total_agents: int = Field(alias="totalAgents")
    total_storage: float | int = Field(alias="totalStorage")


class UsageResponse(BaseModel):
    """Response model for usage metrics."""

    usage: list[UsageMetricOut]
    aggregated: UsageAggregateOut


class UrlResponse(BaseModel):
    """Response model containing a URL."""

    url: str


class AdminStatusOut(BaseModel):
    """Admin status response model."""

    is_admin: bool


class StatusResponse(BaseModel):
    """Generic status response model."""

    status: str


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str
    supabase: bool
    stripe: bool


class SyncUpdateOut(BaseModel):
    """Sync update information model."""

    instance_id: int | str
    old_status: str
    new_status: str
    reason: str


class SyncResult(BaseModel):
    """Sync operation result model."""

    total: int
    synced: int
    errors: int
    updates: list[SyncUpdateOut]


class AdminStatsOut(BaseModel):
    """Admin statistics output model."""

    accounts: int
    active_subscriptions: int
    running_instances: int


class UpdateAccountStatusResponse(BaseModel):
    """Update account status response model."""

    status: str
    account_id: str
    new_status: str


# Account Models
class AccountWithRelationsOut(BaseModel):
    """Account with subscriptions and instances."""

    id: str
    email: str
    full_name: str | None = None  # Genuinely optional
    company_name: str | None = None  # Genuinely optional
    is_admin: bool  # Required, no default
    status: str  # Required, no default
    stripe_customer_id: str | None = None  # Optional for free users
    created_at: str  # Required, should always exist
    updated_at: str  # Required, should always exist
    subscriptions: list[dict[str, Any]] | None = None  # Can be None from DB
    instances: list[dict[str, Any]] | None = None  # Can be None from DB


class AccountSetupResponse(BaseModel):
    """Account setup response model."""

    message: str
    account_id: str
    subscription: SubscriptionOut | None = None


# Subscription Models
class SubscriptionCancelResponse(BaseModel):
    """Subscription cancellation response model."""

    success: bool
    message: str
    cancel_at_period_end: bool | None = None
    subscription_id: str | None = None
    cancelled_at: str | None = None


class SubscriptionReactivateResponse(BaseModel):
    """Subscription reactivation response model."""

    success: bool
    message: str
    subscription_id: str | None = None


# Pricing Models
class StripePriceResponse(BaseModel):
    """Stripe price ID response model."""

    price_id: str
    plan: str
    billing_cycle: str


# Admin Models
class AdminListResponse(BaseModel):
    """Admin list response for generic resources."""

    data: list[dict[str, Any]]
    total: int


class AdminGetOneResponse(BaseModel):
    """Admin single item response."""

    data: dict[str, Any]


class AdminCreateResponse(BaseModel):
    """Admin create response."""

    data: dict[str, Any] | None


class AdminUpdateResponse(BaseModel):
    """Admin update response."""

    data: dict[str, Any] | None


class AdminDeleteResponse(BaseModel):
    """Admin delete response."""

    data: dict[str, Any]


class AdminAccountDetailsResponse(BaseModel):
    """Admin account details response."""

    account: dict[str, Any]
    subscription: dict[str, Any] | None
    instances: list[dict[str, Any]]


class AdminDashboardMetricsResponse(BaseModel):
    """Admin dashboard metrics response."""

    total_accounts: int
    active_subscriptions: int
    total_instances: int
    instances_by_status: dict[str, int]
    subscription_revenue: float
    subscriptions_by_tier: dict[str, int]
    recent_signups: list[dict[str, Any]]
    recent_instances: list[dict[str, Any]]


class AdminLogoutResponse(BaseModel):
    """Admin logout response model."""

    success: bool


# Webhook Models
class WebhookResponse(BaseModel):
    """Webhook processing response model."""

    received: bool
    error: str | None = None
