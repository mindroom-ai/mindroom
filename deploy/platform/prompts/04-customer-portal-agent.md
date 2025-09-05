# Agent 4: Customer Portal Frontend

## Project Context

You are working on MindRoom, an AI agent platform that provides AI assistants to customers via a SaaS model. Customers need a self-service portal to manage their subscription and access their MindRoom instance.

### Understanding MindRoom

First, read these files to understand what customers are getting:
1. `README.md` - The product vision and value proposition
2. `frontend/src/App.tsx` - See the existing configuration UI style
3. `config.yaml` - Understand the agents and features available

### The Goal

Build a beautiful, modern customer portal where users can:
- Sign up and log in
- View their MindRoom instance status and URL
- Manage their subscription via Stripe
- View usage metrics and limits
- Get support

This will be a Next.js 14 app using the App Router, TypeScript, Tailwind CSS, and Supabase for all backend operations.

## Your Specific Task

You will work ONLY in the `apps/customer-portal/` directory to build the customer-facing portal.

### Step 1: Initialize Next.js Project

```bash
cd apps
npx create-next-app@latest customer-portal --typescript --tailwind --app --src-dir --import-alias "@/*"
cd customer-portal
npm install @supabase/supabase-js @supabase/auth-ui-react @supabase/auth-ui-shared
npm install @stripe/stripe-js
npm install recharts lucide-react
npm install @radix-ui/react-dialog @radix-ui/react-tabs @radix-ui/react-dropdown-menu
```

### Step 2: Project Structure

```
apps/customer-portal/
├── src/
│   ├── app/
│   │   ├── layout.tsx              # Root layout with providers
│   │   ├── page.tsx               # Landing page
│   │   ├── globals.css            # Tailwind styles
│   │   ├── auth/
│   │   │   ├── login/
│   │   │   │   └── page.tsx      # Login page
│   │   │   ├── signup/
│   │   │   │   └── page.tsx      # Signup page
│   │   │   ├── callback/
│   │   │   │   └── route.ts      # Auth callback handler
│   │   │   └── reset-password/
│   │   │       └── page.tsx      # Password reset
│   │   ├── dashboard/
│   │   │   ├── layout.tsx        # Dashboard layout
│   │   │   ├── page.tsx          # Main dashboard
│   │   │   ├── instance/
│   │   │   │   └── page.tsx      # Instance management
│   │   │   ├── billing/
│   │   │   │   └── page.tsx      # Billing & subscription
│   │   │   ├── usage/
│   │   │   │   └── page.tsx      # Usage metrics
│   │   │   ├── settings/
│   │   │   │   └── page.tsx      # Account settings
│   │   │   └── support/
│   │   │       └── page.tsx      # Support tickets
│   │   └── api/
│   │       ├── stripe/
│   │       │   └── portal/
│   │       │       └── route.ts  # Create Stripe portal session
│   │       └── instance/
│   │           └── restart/
│   │               └── route.ts  # Restart instance
│   ├── components/
│   │   ├── ui/                    # Reusable UI components
│   │   │   ├── button.tsx
│   │   │   ├── card.tsx
│   │   │   ├── dialog.tsx
│   │   │   ├── tabs.tsx
│   │   │   └── ...
│   │   ├── auth/
│   │   │   ├── AuthForm.tsx      # Supabase Auth UI
│   │   │   └── AuthGuard.tsx     # Protected route wrapper
│   │   ├── dashboard/
│   │   │   ├── Sidebar.tsx       # Dashboard navigation
│   │   │   ├── Header.tsx        # Top header with user menu
│   │   │   ├── InstanceCard.tsx  # Instance status card
│   │   │   ├── UsageChart.tsx    # Usage metrics chart
│   │   │   └── QuickActions.tsx  # Common action buttons
│   │   └── landing/
│   │       ├── Hero.tsx          # Landing page hero
│   │       ├── Features.tsx      # Feature showcase
│   │       └── Pricing.tsx       # Pricing tiers
│   ├── lib/
│   │   ├── supabase/
│   │   │   ├── client.ts         # Supabase client (browser)
│   │   │   ├── server.ts         # Supabase client (server)
│   │   │   └── types.ts          # Database types
│   │   ├── stripe/
│   │   │   └── client.ts         # Stripe.js client
│   │   └── utils.ts               # Utility functions
│   └── hooks/
│       ├── useAuth.ts             # Authentication hook
│       ├── useInstance.ts         # Instance data hook
│       ├── useSubscription.ts     # Subscription data hook
│       └── useUsage.ts            # Usage metrics hook
├── public/
│   ├── logo.svg
│   └── images/
├── .env.local.example
├── next.config.js
├── tailwind.config.ts
└── README.md
```

### Step 3: Core Implementation

#### A. `src/lib/supabase/client.ts` - Supabase Client
```typescript
import { createBrowserClient } from '@supabase/ssr'
import type { Database } from './types'

export function createClient() {
  return createBrowserClient<Database>(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
  )
}
```

#### B. `src/app/layout.tsx` - Root Layout
```typescript
import { Inter } from 'next/font/google'
import './globals.css'

const inter = Inter({ subsets: ['latin'] })

export const metadata = {
  title: 'MindRoom - Your AI Agent Platform',
  description: 'Deploy AI agents that work across all your communication platforms',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className={inter.className}>
      <body className="min-h-screen bg-gradient-to-br from-amber-50 via-orange-50/40 to-yellow-50/50">
        {children}
      </body>
    </html>
  )
}
```

#### C. `src/app/page.tsx` - Landing Page
```typescript
import Link from 'next/link'
import { Hero } from '@/components/landing/Hero'
import { Features } from '@/components/landing/Features'
import { Pricing } from '@/components/landing/Pricing'

export default function LandingPage() {
  return (
    <main className="min-h-screen">
      {/* Navigation */}
      <nav className="fixed top-0 w-full bg-white/80 backdrop-blur-xl border-b z-50">
        <div className="container mx-auto px-6 py-4 flex justify-between items-center">
          <div className="flex items-center gap-2">
            <span className="text-3xl">🧠</span>
            <span className="text-2xl font-bold">MindRoom</span>
          </div>
          <div className="flex gap-4">
            <Link href="/auth/login" className="px-4 py-2 text-gray-600 hover:text-gray-900">
              Sign In
            </Link>
            <Link href="/auth/signup" className="px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600">
              Get Started
            </Link>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <Hero />

      {/* Features */}
      <Features />

      {/* Pricing */}
      <Pricing />

      {/* Footer */}
      <footer className="bg-gray-900 text-white py-12">
        <div className="container mx-auto px-6 text-center">
          <p>© 2024 MindRoom. Your AI, everywhere.</p>
        </div>
      </footer>
    </main>
  )
}
```

#### D. `src/app/auth/login/page.tsx` - Login Page
```typescript
'use client'

import { Auth } from '@supabase/auth-ui-react'
import { ThemeSupa } from '@supabase/auth-ui-shared'
import { createClient } from '@/lib/supabase/client'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'

export default function LoginPage() {
  const supabase = createClient()
  const router = useRouter()

  useEffect(() => {
    // Check if already logged in
    supabase.auth.onAuthStateChange((event, session) => {
      if (session) {
        router.push('/dashboard')
      }
    })
  }, [router])

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="max-w-md w-full bg-white rounded-2xl shadow-xl p-8">
        <div className="text-center mb-8">
          <span className="text-5xl">🧠</span>
          <h1 className="text-3xl font-bold mt-4">Welcome Back</h1>
          <p className="text-gray-600 mt-2">Sign in to access your MindRoom</p>
        </div>

        <Auth
          supabaseClient={supabase}
          appearance={{
            theme: ThemeSupa,
            variables: {
              default: {
                colors: {
                  brand: '#f97316',
                  brandAccent: '#ea580c',
                },
              },
            },
          }}
          providers={['google', 'github']}
          redirectTo={`${window.location.origin}/auth/callback`}
        />
      </div>
    </div>
  )
}
```

#### E. `src/app/dashboard/page.tsx` - Main Dashboard
```typescript
'use client'

import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { InstanceCard } from '@/components/dashboard/InstanceCard'
import { UsageChart } from '@/components/dashboard/UsageChart'
import { QuickActions } from '@/components/dashboard/QuickActions'
import { Loader2 } from 'lucide-react'

export default function DashboardPage() {
  const { user } = useAuth()
  const { instance, loading: instanceLoading } = useInstance()
  const { subscription } = useSubscription()

  if (instanceLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Welcome Header */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h1 className="text-2xl font-bold">Welcome back, {user?.email}!</h1>
        <p className="text-gray-600 mt-1">
          Your MindRoom is {instance?.status === 'running' ? 'up and running' : 'starting up'}
        </p>
      </div>

      {/* Instance Status */}
      <div className="grid md:grid-cols-2 gap-6">
        <InstanceCard instance={instance} />
        <QuickActions instance={instance} />
      </div>

      {/* Usage Overview */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4">Usage This Month</h2>
        <UsageChart subscription={subscription} />
      </div>
    </div>
  )
}
```

#### F. `src/components/dashboard/InstanceCard.tsx` - Instance Status Card
```typescript
import { ExternalLink, CheckCircle, AlertCircle, Loader2 } from 'lucide-react'
import Link from 'next/link'

export function InstanceCard({ instance }: { instance: any }) {
  const getStatusIcon = () => {
    switch (instance?.status) {
      case 'running':
        return <CheckCircle className="w-5 h-5 text-green-500" />
      case 'provisioning':
        return <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
      case 'failed':
        return <AlertCircle className="w-5 h-5 text-red-500" />
      default:
        return <AlertCircle className="w-5 h-5 text-gray-400" />
    }
  }

  const getStatusText = () => {
    switch (instance?.status) {
      case 'running':
        return 'Running'
      case 'provisioning':
        return 'Setting up your MindRoom...'
      case 'failed':
        return 'Setup failed - Please contact support'
      default:
        return 'Unknown'
    }
  }

  if (!instance) {
    return (
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4">Your MindRoom Instance</h2>
        <div className="text-center py-8">
          <Loader2 className="w-8 h-8 mx-auto text-gray-400 animate-spin" />
          <p className="text-gray-500 mt-4">Setting up your instance...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-white rounded-lg p-6 shadow-sm">
      <h2 className="text-xl font-bold mb-4">Your MindRoom Instance</h2>

      <div className="space-y-4">
        {/* Status */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Status</span>
          <div className="flex items-center gap-2">
            {getStatusIcon()}
            <span className="font-medium">{getStatusText()}</span>
          </div>
        </div>

        {/* URL */}
        {instance.frontend_url && (
          <div className="flex items-center justify-between">
            <span className="text-gray-600">URL</span>
            <Link
              href={instance.frontend_url}
              target="_blank"
              className="flex items-center gap-1 text-blue-600 hover:text-blue-700"
            >
              Open MindRoom
              <ExternalLink className="w-4 h-4" />
            </Link>
          </div>
        )}

        {/* Subdomain */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Subdomain</span>
          <span className="font-mono text-sm">{instance.subdomain}</span>
        </div>

        {/* Created */}
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Created</span>
          <span className="text-sm">
            {new Date(instance.created_at).toLocaleDateString()}
          </span>
        </div>
      </div>
    </div>
  )
}
```

#### G. `src/app/dashboard/billing/page.tsx` - Billing Page
```typescript
'use client'

import { useState } from 'react'
import { useSubscription } from '@/hooks/useSubscription'
import { loadStripe } from '@stripe/stripe-js'
import { Loader2, CreditCard, TrendingUp } from 'lucide-react'

const stripePromise = loadStripe(process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY!)

export default function BillingPage() {
  const { subscription, loading } = useSubscription()
  const [redirecting, setRedirecting] = useState(false)

  const openStripePortal = async () => {
    setRedirecting(true)

    // Call API to create portal session
    const response = await fetch('/api/stripe/portal', {
      method: 'POST',
    })

    const { url } = await response.json()
    window.location.href = url
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="w-8 h-8 animate-spin" />
      </div>
    )
  }

  const getTierDisplay = (tier: string) => {
    const tiers: { [key: string]: { name: string; price: string; color: string } } = {
      free: { name: 'Free', price: '$0/month', color: 'gray' },
      starter: { name: 'Starter', price: '$49/month', color: 'blue' },
      professional: { name: 'Professional', price: '$199/month', color: 'purple' },
      enterprise: { name: 'Enterprise', price: 'Custom', color: 'gold' },
    }
    return tiers[tier] || tiers.free
  }

  const tierInfo = getTierDisplay(subscription?.tier || 'free')

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Billing & Subscription</h1>

      {/* Current Plan */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-xl font-bold mb-2">Current Plan</h2>
            <div className="flex items-center gap-3">
              <span className={`px-3 py-1 rounded-full text-sm font-medium bg-${tierInfo.color}-100 text-${tierInfo.color}-700`}>
                {tierInfo.name}
              </span>
              <span className="text-2xl font-bold">{tierInfo.price}</span>
            </div>
          </div>

          <button
            onClick={openStripePortal}
            disabled={redirecting}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            {redirecting ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <CreditCard className="w-4 h-4" />
            )}
            Manage Subscription
          </button>
        </div>

        {/* Plan Details */}
        <div className="mt-6 pt-6 border-t">
          <h3 className="font-semibold mb-3">Plan Includes:</h3>
          <div className="grid md:grid-cols-2 gap-4">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400" />
              <span>{subscription?.max_agents || 1} AI Agents</span>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400" />
              <span>{subscription?.max_messages_per_day || 100} Messages/Day</span>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400" />
              <span>{subscription?.max_storage_gb || 1}GB Storage</span>
            </div>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-gray-400" />
              <span>Priority Support</span>
            </div>
          </div>
        </div>

        {/* Billing Period */}
        {subscription?.current_period_end && (
          <div className="mt-6 pt-6 border-t">
            <p className="text-sm text-gray-600">
              Next billing date: {new Date(subscription.current_period_end).toLocaleDateString()}
            </p>
          </div>
        )}
      </div>

      {/* Payment Method */}
      <div className="bg-white rounded-lg p-6 shadow-sm">
        <h2 className="text-xl font-bold mb-4">Payment Method</h2>
        <p className="text-gray-600 mb-4">
          Manage your payment methods and billing information through the Stripe customer portal.
        </p>
        <button
          onClick={openStripePortal}
          className="text-blue-600 hover:text-blue-700 font-medium"
        >
          Update Payment Method →
        </button>
      </div>
    </div>
  )
}
```

#### H. `src/hooks/useInstance.ts` - Instance Data Hook
```typescript
import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { useAuth } from './useAuth'

export function useInstance() {
  const [instance, setInstance] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const { user } = useAuth()
  const supabase = createClient()

  useEffect(() => {
    if (!user) return

    // Get user's instance
    const fetchInstance = async () => {
      const { data, error } = await supabase
        .from('instances')
        .select(`
          *,
          subscriptions (
            tier,
            max_agents,
            max_messages_per_day
          )
        `)
        .eq('subscriptions.account_id', user.id)
        .single()

      if (data) {
        setInstance(data)
      }
      setLoading(false)
    }

    fetchInstance()

    // Subscribe to changes
    const subscription = supabase
      .channel('instance-changes')
      .on(
        'postgres_changes',
        {
          event: '*',
          schema: 'public',
          table: 'instances',
          filter: `subscription_id=eq.${user.id}`,
        },
        (payload) => {
          setInstance(payload.new)
        }
      )
      .subscribe()

    return () => {
      subscription.unsubscribe()
    }
  }, [user])

  return { instance, loading }
}
```

### Step 4: Landing Page Components

Create beautiful landing page components in `src/components/landing/`:

- **Hero.tsx**: Eye-catching hero section with value proposition
- **Features.tsx**: Feature cards showcasing MindRoom capabilities
- **Pricing.tsx**: Pricing tiers with Stripe checkout integration

### Step 5: Environment Variables

Create `.env.local.example`:
```bash
# Supabase
NEXT_PUBLIC_SUPABASE_URL=https://xxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...

# Stripe
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...

# URLs
NEXT_PUBLIC_APP_URL=http://localhost:3000
```

## Design Guidelines

1. **Color Scheme**: Use warm colors (amber, orange, yellow) matching the existing MindRoom brand
2. **Typography**: Clean, modern fonts with good readability
3. **Icons**: Use Lucide React for consistent iconography
4. **Animations**: Subtle transitions and loading states
5. **Responsive**: Mobile-first design that works on all devices
6. **Accessibility**: WCAG 2.1 AA compliant

## Key Features to Implement

1. **Authentication**:
   - Email/password signup
   - Social login (Google, GitHub)
   - Password reset
   - Email verification

2. **Dashboard**:
   - Instance status with real-time updates
   - Quick access to MindRoom
   - Usage metrics visualization
   - Recent activity feed

3. **Billing**:
   - Current plan display
   - Stripe Customer Portal integration
   - Usage vs limits
   - Upgrade/downgrade flows

4. **Support**:
   - In-app support tickets
   - FAQ section
   - Documentation links
   - Status page

## Output Files Required

```
apps/customer-portal/
├── src/
│   ├── app/
│   ├── components/
│   ├── lib/
│   ├── hooks/
│   └── types/
├── public/
├── .env.local.example
├── next.config.js
├── tailwind.config.ts
├── package.json
└── README.md
```

## Important Notes

1. DO NOT modify any files outside `apps/customer-portal/`
2. Use Supabase for ALL backend operations - no custom API needed
3. Implement proper error handling and loading states
4. Add proper TypeScript types for all data
5. Follow Next.js 14 App Router best practices
6. Use React Server Components where possible
7. Implement proper SEO with metadata

Remember: This is the face of MindRoom for customers. Make it beautiful, fast, and delightful to use!
