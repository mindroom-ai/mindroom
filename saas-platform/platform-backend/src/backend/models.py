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
    status: Literal["provisioning", "running", "stopped", "failed", "error", "deprovisioned", "restarting"]
    frontend_url: str | None = None
    backend_url: str | None = None
    matrix_server_url: str | None = None
    tier: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    kubernetes_synced_at: str | None = None


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
    max_storage_gb: int | None = None
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
class AccountOut(BaseModel):
    """Account information output model."""

    id: str
    email: str
    full_name: str | None = None
    company_name: str | None = None
    is_admin: bool = False
    status: str = "active"
    stripe_customer_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AccountSetupResponse(BaseModel):
    """Account setup response model."""

    message: str
    account_id: str


# Subscription Models
class SubscriptionCancelResponse(BaseModel):
    """Subscription cancellation response model."""

    success: bool
    message: str
    cancelled_at: str | None = None


class SubscriptionReactivateResponse(BaseModel):
    """Subscription reactivation response model."""

    success: bool
    message: str
    subscription_id: str | None = None


# Pricing Models
class PricingPlanOut(BaseModel):
    """Individual pricing plan model."""

    model_config = {"populate_by_name": True}

    name: str
    price: int
    period: str
    features: list[str]
    is_popular: bool = Field(default=False, alias="isPopular")
    stripe_price_id_monthly: str | None = None
    stripe_price_id_yearly: str | None = None


class PricingConfigResponse(BaseModel):
    """Pricing configuration response model."""

    plans: dict[str, PricingPlanOut]


class StripePriceResponse(BaseModel):
    """Stripe price ID response model."""

    price_id: str
    plan: str
    billing_cycle: str


# Admin Models
class AdminStatsDetailedOut(BaseModel):
    """Detailed admin statistics output model."""

    accounts_count: int
    subscriptions_count: int
    instances_count: int
    recent_activity: list[dict[str, Any]]


class AdminAccountDetailOut(BaseModel):
    """Detailed account information for admin view."""

    id: str
    email: str
    full_name: str | None = None
    company_name: str | None = None
    is_admin: bool = False
    status: str = "active"
    stripe_customer_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    subscription: SubscriptionOut | None = None
    instances: list[InstanceOut] = []


class AdminProvisionResponse(BaseModel):
    """Admin provision response model."""

    success: bool
    message: str
    instance_id: int


class AdminSyncResponse(BaseModel):
    """Admin sync response model."""

    success: bool
    message: str
    synced: int
    errors: int


class AdminLogoutResponse(BaseModel):
    """Admin logout response model."""

    success: bool


# React Admin Models
class ReactAdminListResponse(BaseModel):
    """React Admin list response model."""

    data: list[dict[str, Any]]
    total: int


class ReactAdminItemResponse(BaseModel):
    """React Admin single item response model."""

    data: dict[str, Any] | None


class ReactAdminDeleteResponse(BaseModel):
    """React Admin delete response model."""

    data: dict[str, Any]


# Dashboard Metrics Models
class DashboardMetricsOut(BaseModel):
    """Dashboard metrics model."""

    model_config = {"populate_by_name": True}

    total_accounts: int = Field(alias="totalAccounts")
    active_subscriptions: int = Field(alias="activeSubscriptions")
    running_instances: int = Field(alias="runningInstances")
    mrr: int
    daily_messages: list[dict[str, Any]] = Field(alias="dailyMessages")
    instance_statuses: list[dict[str, Any]] = Field(alias="instanceStatuses")
    recent_activity: list[dict[str, Any]] = Field(alias="recentActivity")


# Webhook Models
class WebhookResponse(BaseModel):
    """Webhook processing response model."""

    received: bool


# Checkout Models
class CheckoutSessionRequest(BaseModel):
    """Checkout session request model."""

    price_id: str
    billing_cycle: Literal["monthly", "yearly"]
    user_count: int = 1


class CheckoutSessionResponse(BaseModel):
    """Checkout session response model."""

    url: str
    session_id: str
