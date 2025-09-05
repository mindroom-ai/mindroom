import { TierName } from '../config';

// Database models (matching Supabase schema)
export interface Account {
  id: string;
  email: string;
  stripe_customer_id: string;
  created_at: Date;
  updated_at: Date;
}

export interface Subscription {
  id: string;
  account_id: string;
  stripe_subscription_id: string;
  stripe_price_id: string;
  tier: TierName;
  status: 'active' | 'trialing' | 'past_due' | 'cancelled' | 'incomplete' | 'incomplete_expired' | 'unpaid';
  max_agents: number;
  max_messages_per_day: number;
  current_period_start: Date;
  current_period_end: Date;
  trial_ends_at?: Date | null;
  cancelled_at?: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface Instance {
  id: string;
  subscription_id: string;
  dokku_app_name: string;
  subdomain: string;
  status: 'provisioning' | 'active' | 'deprovisioning' | 'failed' | 'stopped';
  frontend_url: string;
  backend_url: string;
  memory_limit_mb: number;
  cpu_limit: number;
  deprovisioned_at?: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface UsageRecord {
  id: string;
  subscription_id: string;
  date: Date;
  messages_sent: number;
  agents_active: number;
  api_calls: number;
  created_at: Date;
}

export interface WebhookEvent {
  id: string;
  stripe_event_id: string;
  event_type: string;
  payload: any;
  processed_at?: Date | null;
  error?: string | null;
  created_at: Date;
}

// Provisioner API interfaces
export interface ProvisionRequest {
  subscriptionId: string;
  accountId: string;
  tier: TierName;
  limits: ResourceLimits;
  subdomain?: string;
}

export interface ProvisionResponse {
  appName: string;
  subdomain: string;
  frontendUrl: string;
  backendUrl: string;
  status: 'success' | 'pending';
  message?: string;
}

export interface DeprovisionRequest {
  appName: string;
}

export interface DeprovisionResponse {
  status: 'success' | 'pending';
  message?: string;
}

export interface UpdateLimitsRequest {
  appName: string;
  limits: ResourceLimits;
}

export interface UpdateLimitsResponse {
  status: 'success' | 'failed';
  message?: string;
}

export interface ResourceLimits {
  agents: number;
  messagesPerDay: number;
  memoryMb: number;
  cpuLimit: number;
}

// Email interfaces
export interface EmailData {
  to: string;
  subject: string;
  template: 'welcome' | 'trial_ending' | 'payment_failed' | 'subscription_cancelled' | 'subscription_upgraded' | 'subscription_downgraded';
  data: Record<string, any>;
}

// Internal processing interfaces
export interface ProcessingResult {
  success: boolean;
  message: string;
  data?: any;
  error?: Error;
}

export interface WebhookProcessingContext {
  eventId: string;
  timestamp: Date;
  retryCount: number;
  customerId?: string;
  subscriptionId?: string;
}

// Utility types
export type StripeEventType =
  | 'customer.subscription.created'
  | 'customer.subscription.updated'
  | 'customer.subscription.deleted'
  | 'customer.subscription.trial_will_end'
  | 'invoice.payment_succeeded'
  | 'invoice.payment_failed'
  | 'invoice.upcoming'
  | 'customer.created'
  | 'customer.updated'
  | 'customer.deleted'
  | 'payment_method.attached'
  | 'payment_method.detached';

export interface ApiError extends Error {
  statusCode?: number;
  code?: string;
  details?: any;
}
