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
- **Framework**: Next.js 16 with App Router (see `package.json` for the version)
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
- Custom Next.js and React admin components
- Customer management interface
- Instance lifecycle control
- Platform metrics and monitoring

### Project Structure

```
src/app/             # App Router pages, auth callback, and CSP-report route
src/components/      # Reusable React components
src/lib/             # API, runtime configuration, and authentication helpers
public/              # Static assets
```

## Security

- JWT-based authentication via Supabase
- Server-side session validation
- Admin page guards in `proxy.ts` and `src/app/admin/layout.tsx`
- Independent authentication and authorization dependencies in the platform backend
- Environment variable separation for secrets

## Development

Runs on port 3000 by default with hot module replacement.

### Typed API client

Ordinary browser data requests use `src/lib/api.ts` to call the configured platform API directly with the current session's bearer token.
The Next.js server also handles session validation, admin page guards, the authentication callback, and CSP reports.
`src/lib/api.ts` is a thin typed fetch wrapper whose request and response types come from `src/lib/api.generated.ts`, generated with `openapi-typescript` from the backend's OpenAPI schema (`../platform-backend/openapi.json`).
After changing backend routes or response models, regenerate both files with `just saas-openapi` from the repo root (exports the schema, then runs `bun run generate:api` here) and commit the result.
`bun run check:api` fails when `api.generated.ts` is stale relative to the committed schema; run it in CI or before pushing backend-facing changes.

## Environment Variables

Required for authentication and request handling:

- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_ANON_KEY` - Public anon key

Optional runtime configuration:

- `PLATFORM_DOMAIN` - Derives the API origin as `https://api.<domain>`; when unset or empty, the API origin is `http://localhost:8000`.

The runtime configuration is serialized into the browser by the root layout.
The request proxy and authentication helpers require the Supabase URL and anon key.
