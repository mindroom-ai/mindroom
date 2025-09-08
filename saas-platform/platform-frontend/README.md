# MindRoom Customer Portal

A beautiful, modern customer portal for MindRoom - the AI agent platform that deploys intelligent assistants across all communication channels.

## 🚀 Features

### Landing Page
- Eye-catching hero section with clear value proposition
- Feature showcase highlighting MindRoom capabilities
- Transparent pricing tiers
- Responsive design optimized for all devices

### Authentication
- Email/password authentication
- Social login (Google, GitHub)
- Secure password reset flow
- Email verification

### Dashboard
- **Overview**: Real-time instance status and quick actions
- **Instance Management**: Monitor and control your MindRoom instance
- **Billing**: Manage subscription and payment methods via Stripe
- **Usage Analytics**: Track messages, agents, and storage usage
- **Settings**: Account and preference management
- **Support**: In-app support ticket system

## 🛠️ Tech Stack

- **Framework**: Next.js 14 with App Router
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **Authentication**: Supabase Auth
- **Database**: Supabase (PostgreSQL)
- **Payments**: Stripe
- **Charts**: Recharts
- **Icons**: Lucide React
- **UI Components**: Custom components with Radix UI

## 📁 Project Structure

```
platform-frontend/
├── src/
│   ├── app/                    # Next.js App Router pages
│   │   ├── auth/               # Authentication pages
│   │   ├── dashboard/          # Dashboard pages
│   │   └── api/                # API routes
│   ├── components/             # React components
│   │   ├── ui/                 # Reusable UI components
│   │   ├── auth/               # Auth-specific components
│   │   ├── dashboard/          # Dashboard components
│   │   └── landing/            # Landing page components
│   ├── lib/                    # Utility libraries
│   │   ├── supabase/           # Supabase client config
│   │   └── stripe/             # Stripe configuration
│   └── hooks/                  # Custom React hooks
├── public/                     # Static assets
├── .env.local.example          # Environment variables template
└── package.json                # Dependencies
```

## 🚀 Getting Started

### Prerequisites

- Node.js 18+ and npm
- Supabase account and project
- Stripe account (for payment processing)

### Installation

1. Install dependencies:
```bash
npm install
```

2. Set up environment variables:
```bash
cp .env.local.example .env.local
```

3. Configure your `.env.local` file:
```env
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key

# Stripe
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...

# App URL
APP_URL=http://localhost:3000
```

4. Run the development server:
```bash
npm run dev
```

5. Open [http://localhost:3000](http://localhost:3000) in your browser

## 🗄️ Database Schema

### Required Supabase Tables

```sql
-- Accounts table
CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Subscriptions table
CREATE TABLE subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id),
  tier TEXT DEFAULT 'free',
  status TEXT DEFAULT 'active',
  stripe_subscription_id TEXT,
  stripe_customer_id TEXT,
  current_period_end TIMESTAMPTZ,
  max_agents INTEGER DEFAULT 1,
  max_messages_per_day INTEGER DEFAULT 100,
  max_storage_gb INTEGER DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Instances table
CREATE TABLE instances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id UUID REFERENCES subscriptions(id),
  subdomain TEXT UNIQUE NOT NULL,
  status TEXT DEFAULT 'provisioning',
  frontend_url TEXT,
  backend_url TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Usage metrics table
CREATE TABLE usage_metrics (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id UUID REFERENCES subscriptions(id),
  date DATE NOT NULL,
  messages_sent INTEGER DEFAULT 0,
  agents_used INTEGER DEFAULT 0,
  storage_used_gb DECIMAL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

## 🎨 Design System

### Color Palette
- **Primary**: Orange (`#f97316`)
- **Background**: Gradient from amber to yellow
- **Text**: Gray scale
- **Success**: Green
- **Warning**: Yellow
- **Error**: Red

### Typography
- **Font**: Inter
- **Headings**: Bold, varied sizes
- **Body**: Regular, readable sizes

### Components
- Cards with subtle shadows
- Rounded corners (8px default)
- Smooth transitions
- Loading states with spinners
- Empty states with helpful messages

## 🔐 Security

- Server-side authentication with Supabase
- Row Level Security (RLS) policies
- Secure API routes with auth checks
- HTTPS in production
- Environment variables for sensitive data
- Stripe webhook signature verification

## 📝 Key Features Implementation

### Real-time Updates
Uses Supabase real-time subscriptions to update:
- Instance status changes
- Subscription updates
- Usage metrics

### Responsive Design
- Mobile-first approach
- Breakpoints: sm (640px), md (768px), lg (1024px), xl (1280px)
- Touch-friendly interfaces

### Performance
- React Server Components where possible
- Image optimization with Next.js Image
- Code splitting and lazy loading
- Efficient data fetching with hooks

## 🚢 Deployment

### Production Build
```bash
npm run build
npm start
```

### Environment Variables for Production
- Update `APP_URL` to your production domain
- Use production Supabase and Stripe keys
- Enable Supabase RLS policies
- Configure Stripe webhooks

## 📄 License

Copyright © 2024 MindRoom. All rights reserved.
