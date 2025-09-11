# Development Authentication Bypass

## Quick Start

To bypass authentication during development:

1. Create a `.env.local` file in the `platform-frontend` directory
2. Add the following line:
   ```
   NEXT_PUBLIC_DEV_AUTH=true
   ```
3. Start the development server: `pnpm run dev`
4. You can now access the dashboard without logging in

## How It Works

When `NEXT_PUBLIC_DEV_AUTH=true` is set in development mode:
- A mock user (`dev@mindroom.local`) is automatically logged in
- A mock instance with sample data is displayed
- No API calls are made to the backend
- Sign out just redirects to the homepage

## Important Notes

- **This ONLY works in development mode** (`NODE_ENV=development`)
- **NEVER use this in production** - it won't work anyway
- The `.env.local` file is gitignored and should never be committed
- To disable dev auth, remove the line from `.env.local` or set it to `false`

## Mock Data

The mock instance shows:
- Status: Running
- Tier: Starter
- Sample URLs for frontend, backend, and Matrix server
- Creation date: 7 days ago
- Last update: 1 hour ago

This allows you to test the full dashboard UI without a real backend.
