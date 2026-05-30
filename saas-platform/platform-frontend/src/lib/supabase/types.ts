// Database types for Supabase
type PlanTier = 'free' | 'byok' | 'hobby' | 'pro' | 'enterprise'
type AccountStatus = 'active' | 'suspended' | 'deleted' | 'pending_verification'
type SubscriptionStatus = 'trialing' | 'active' | 'cancelled' | 'past_due' | 'paused'

export type Database = {
  public: {
    Tables: {
      accounts: {
        Row: {
          id: string
          email: string
          full_name: string | null
          company_name: string | null
          stripe_customer_id: string | null
          tier: PlanTier
          is_admin: boolean
          status: AccountStatus
          deleted_at: string | null
          deletion_reason: string | null
          deletion_requested_by: string | null
          deletion_requested_at: string | null
          consent_marketing: boolean
          consent_analytics: boolean
          consent_updated_at: string | null
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          email: string
          full_name?: string | null
          company_name?: string | null
          stripe_customer_id?: string | null
          tier?: PlanTier
          is_admin?: boolean
          status?: AccountStatus
          deleted_at?: string | null
          deletion_reason?: string | null
          deletion_requested_by?: string | null
          deletion_requested_at?: string | null
          consent_marketing?: boolean
          consent_analytics?: boolean
          consent_updated_at?: string | null
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          email?: string
          full_name?: string | null
          company_name?: string | null
          stripe_customer_id?: string | null
          tier?: PlanTier
          is_admin?: boolean
          status?: AccountStatus
          deleted_at?: string | null
          deletion_reason?: string | null
          deletion_requested_by?: string | null
          deletion_requested_at?: string | null
          consent_marketing?: boolean
          consent_analytics?: boolean
          consent_updated_at?: string | null
          created_at?: string
          updated_at?: string
        }
      }
      subscriptions: {
        Row: {
          id: string
          account_id: string
          subscription_id: string | null
          stripe_subscription_id: string | null
          stripe_price_id: string | null
          tier: PlanTier
          status: SubscriptionStatus
          trial_ends_at: string | null
          current_period_start: string | null
          current_period_end: string | null
          cancelled_at: string | null
          max_agents: number
          max_messages_per_day: number
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          account_id: string
          subscription_id?: string | null
          stripe_subscription_id?: string | null
          stripe_price_id?: string | null
          tier?: PlanTier
          status?: SubscriptionStatus
          trial_ends_at?: string | null
          current_period_start?: string | null
          current_period_end?: string | null
          cancelled_at?: string | null
          max_agents?: number
          max_messages_per_day?: number
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          account_id?: string
          subscription_id?: string | null
          stripe_subscription_id?: string | null
          stripe_price_id?: string | null
          tier?: PlanTier
          status?: SubscriptionStatus
          trial_ends_at?: string | null
          current_period_start?: string | null
          current_period_end?: string | null
          cancelled_at?: string | null
          max_agents?: number
          max_messages_per_day?: number
          created_at?: string
          updated_at?: string
        }
      }
      instances: {
        Row: {
          id: string
          subscription_id: string
          subdomain: string
          status: 'provisioning' | 'running' | 'stopped' | 'error' | 'deprovisioned' | 'restarting'
          frontend_url: string | null
          backend_url: string | null
          openrouter_key_hash: string | null
          openrouter_key_label: string | null
          openrouter_key_limit_usd: number | null
          openrouter_key_limit_reset: string | null
          openrouter_key_created_at: string | null
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          subscription_id: string
          subdomain: string
          status?: 'provisioning' | 'running' | 'stopped' | 'error' | 'deprovisioned' | 'restarting'
          frontend_url?: string | null
          backend_url?: string | null
          openrouter_key_hash?: string | null
          openrouter_key_label?: string | null
          openrouter_key_limit_usd?: number | null
          openrouter_key_limit_reset?: string | null
          openrouter_key_created_at?: string | null
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          subscription_id?: string
          subdomain?: string
          status?: 'provisioning' | 'running' | 'stopped' | 'error' | 'deprovisioned' | 'restarting'
          frontend_url?: string | null
          backend_url?: string | null
          openrouter_key_hash?: string | null
          openrouter_key_label?: string | null
          openrouter_key_limit_usd?: number | null
          openrouter_key_limit_reset?: string | null
          openrouter_key_created_at?: string | null
          created_at?: string
          updated_at?: string
        }
      }
      usage_metrics: {
        Row: {
          id: string
          subscription_id: string
          date: string
          messages_sent: number
          agents_used: number
          storage_used_gb: number
          created_at: string
        }
        Insert: {
          id?: string
          subscription_id: string
          date: string
          messages_sent: number
          agents_used: number
          storage_used_gb: number
          created_at?: string
        }
        Update: {
          id?: string
          subscription_id?: string
          date?: string
          messages_sent?: number
          agents_used?: number
          storage_used_gb?: number
          created_at?: string
        }
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      [_ in never]: never
    }
    Enums: {
      [_ in never]: never
    }
  }
}
