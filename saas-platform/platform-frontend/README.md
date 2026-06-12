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

`src/lib/api.ts` is typed against the platform backend's OpenAPI schema.
`openapi.json` is checked in and `src/lib/api.generated.ts` is generated from it with `openapi-typescript` (types only, no runtime code).

After changing backend routes or response models, regenerate both files:

```bash
just saas-api-types
# or manually:
cd saas-platform/platform-backend && uv run python scripts/export_openapi.py
cd saas-platform/platform-frontend && bun run generate:api-types
```

`bun run check:api-types` is the CI-runnable drift guard: it regenerates the types from the checked-in `openapi.json` and fails if `src/lib/api.generated.ts` is stale.

## Environment Variables

Required for runtime:
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_ANON_KEY` - Public anon key
- `SUPABASE_SERVICE_KEY` - Service key for server-side operations
- `STRIPE_SECRET_KEY` - Stripe API key
- `PLATFORM_BACKEND_URL` - Backend service URL
