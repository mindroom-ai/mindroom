# Platform Frontend

Next.js application for the MindRoom customer portal and admin dashboard.

## Purpose

Customer-facing web application providing:
- User authentication and account management
- Instance configuration and monitoring
- Billing and subscription management
- Admin dashboard for platform management

## Architecture

### Tech Stack
- **Framework**: Next.js 14 with App Router
- **Language**: TypeScript
- **Styling**: Tailwind CSS
- **UI Components**: Custom components with shadcn/ui patterns
- **State Management**: React hooks and context
- **Authentication**: Supabase Auth

### Key Features

**Customer Portal**
- Self-service instance management
- Subscription and billing dashboard
- Account settings and preferences
- Instance health monitoring

**Admin Dashboard**
- React Admin integration
- Customer management interface
- Instance lifecycle control
- Platform metrics and monitoring

### Project Structure

```
app/                  # Next.js app router pages
components/           # Reusable React components
lib/                 # Utilities and client libraries
public/              # Static assets
```

## Security

- JWT-based authentication via Supabase
- Server-side session validation
- Protected API routes with middleware
- Environment variable separation for secrets

## Development

Runs on port 3000 by default with hot module replacement.

### Typed API client

`src/lib/api.ts` is a thin typed fetch wrapper whose request and response types come from `src/lib/api.generated.ts`, generated with `openapi-typescript` from the backend's OpenAPI schema (`../platform-backend/openapi.json`).
After changing backend routes or response models, regenerate both files with `just saas-openapi` from the repo root (exports the schema, then runs `bun run generate:api` here) and commit the result.
`bun run check:api` fails when `api.generated.ts` is stale relative to the committed schema; run it in CI or before pushing backend-facing changes.

## Environment Variables

Required for runtime:
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_ANON_KEY` - Public anon key
- `SUPABASE_SERVICE_KEY` - Service key for server-side operations
- `STRIPE_SECRET_KEY` - Stripe API key
- `PLATFORM_BACKEND_URL` - Backend service URL
