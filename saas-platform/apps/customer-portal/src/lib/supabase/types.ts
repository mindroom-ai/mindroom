// Database types for Supabase
export type Database = {
  public: {
    Tables: {
      accounts: {
        Row: {
          id: string
          email: string
          is_admin: boolean
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          email: string
          is_admin?: boolean
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          email?: string
          is_admin?: boolean
          created_at?: string
          updated_at?: string
        }
      }
      subscriptions: {
        Row: {
          id: string
          account_id: string
          tier: 'free' | 'starter' | 'professional' | 'enterprise'
          status: 'active' | 'cancelled' | 'past_due'
          stripe_subscription_id: string | null
          stripe_customer_id: string | null
          current_period_end: string | null
          max_agents: number
          max_messages_per_day: number
          max_storage_gb: number
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          account_id: string
          tier?: 'free' | 'starter' | 'professional' | 'enterprise'
          status?: 'active' | 'cancelled' | 'past_due'
          stripe_subscription_id?: string | null
          stripe_customer_id?: string | null
          current_period_end?: string | null
          max_agents?: number
          max_messages_per_day?: number
          max_storage_gb?: number
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          account_id?: string
          tier?: 'free' | 'starter' | 'professional' | 'enterprise'
          status?: 'active' | 'cancelled' | 'past_due'
          stripe_subscription_id?: string | null
          stripe_customer_id?: string | null
          current_period_end?: string | null
          max_agents?: number
          max_messages_per_day?: number
          max_storage_gb?: number
          created_at?: string
          updated_at?: string
        }
      }
      instances: {
        Row: {
          id: string
          subscription_id: string
          subdomain: string
          status: 'provisioning' | 'running' | 'failed' | 'stopped'
          frontend_url: string | null
          backend_url: string | null
          created_at: string
          updated_at: string
        }
        Insert: {
          id?: string
          subscription_id: string
          subdomain: string
          status?: 'provisioning' | 'running' | 'failed' | 'stopped'
          frontend_url?: string | null
          backend_url?: string | null
          created_at?: string
          updated_at?: string
        }
        Update: {
          id?: string
          subscription_id?: string
          subdomain?: string
          status?: 'provisioning' | 'running' | 'failed' | 'stopped'
          frontend_url?: string | null
          backend_url?: string | null
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
