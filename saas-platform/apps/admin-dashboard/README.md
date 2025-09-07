# MindRoom Admin Dashboard

Internal admin dashboard for managing MindRoom platform operations, customers, subscriptions, and instances.

## Features

### Customer Management
- View all customers with search and filters
- Edit customer details and status
- Suspend/reactivate accounts
- View customer history and usage metrics
- Manage API keys

### Subscription Management
- View and edit subscriptions
- Manual tier changes and upgrades/downgrades
- Apply credits and discounts
- Handle failed payments
- Monitor usage against limits

### Instance Operations
- Start/stop/restart instances
- Manual provisioning and deprovisioning
- View logs and health metrics
- Resource limit adjustments
- Real-time health monitoring

### Platform Metrics
- Monthly Recurring Revenue (MRR) tracking
- Daily message volume charts
- Instance status distribution
- Active user counts
- API usage metrics

### Audit & Security
- Complete audit log of all admin actions
- IP tracking and request IDs
- Change history with before/after values
- User activity monitoring

## Tech Stack

- **React Admin**: Admin framework with data providers and auth
- **Supabase**: Database and authentication
- **TypeScript**: Type-safe development
- **Tailwind CSS**: Utility-first styling
- **Recharts**: Data visualization
- **Vite**: Fast build tool

## Setup

### Prerequisites

- Node.js 20+
- npm or yarn
- Supabase account with service key
- Access to MindRoom database

### Installation

1. Install dependencies:
```bash
cd apps/admin-dashboard
pnpm install
```

2. Setup environment variables:
```bash
cp .env.example .env
# Edit .env with your actual values
```

3. Start development server:
```bash
pnpm run dev
```

The dashboard will be available at http://localhost:5173

### Environment Variables

For local development, create a `.env` file with:

- `VITE_SUPABASE_URL`: Your Supabase project URL
- `VITE_SUPABASE_SERVICE_KEY`: Service role key for full database access
- `VITE_PROVISIONER_URL`: Instance provisioner API endpoint (default: http://localhost:8002)
- `VITE_PROVISIONER_API_KEY`: API key for provisioner
- `VITE_STRIPE_SECRET_KEY`: Stripe secret key (optional)

## Development

### Project Structure

```
src/
├── resources/          # React Admin resources (CRUD views)
│   ├── accounts/       # Customer management
│   ├── subscriptions/  # Subscription management
│   ├── instances/      # Instance operations
│   └── audit_logs/     # Audit trail
├── components/         # Reusable UI components
├── services/           # API service layers
├── App.tsx            # Main application setup
├── Dashboard.tsx      # Dashboard with metrics
├── dataProvider.ts    # Supabase data provider
└── authProvider.ts    # Authentication provider
```

### Adding New Resources

1. Create resource directory in `src/resources/`
2. Add List, Show, Edit, and Create components as needed
3. Register resource in `App.tsx`
4. Update data provider if special handling needed

### Custom Actions

Instance actions (start/stop/restart) are implemented as custom buttons that call the provisioner API directly. See `InstanceList.tsx` for examples.

## Deployment

### Docker

Build and run with Docker:

```bash
docker build -t mindroom-admin .
docker run -p 80:80 --env-file .env mindroom-admin
```

### Manual Deployment

1. Build for production:
```bash
npm run build
```

2. Serve `dist/` folder with any static file server

## Security

### Authentication
- Uses Supabase Auth with email/password
- Sessions stored in localStorage
- Automatic token refresh

### Authorization
- Service key provides full database access
- Admin-only access enforced at API level
- All actions logged to audit trail

### Best Practices
- Never expose service key to clients
- Use environment variables for secrets
- Implement IP restrictions in production
- Enable 2FA for admin accounts
- Regular security audits

## Monitoring

The dashboard includes real-time monitoring features:

- **Instance Health**: Auto-refreshes every 30 seconds
- **Platform Metrics**: Live MRR and usage tracking
- **Alert System**: Critical issues highlighted
- **Audit Trail**: Complete action history

## API Integration

### Supabase
All database operations go through Supabase using the service key for unrestricted access.

### Dokku Provisioner
Instance management operations call the provisioner API at `/api/instances/*` endpoints.

### Stripe
Payment operations can integrate with Stripe API for subscription management.

## Troubleshooting

### Common Issues

1. **Authentication fails**: Check Supabase URL and service key
2. **Instances won't start/stop**: Verify provisioner API is running
3. **Data not loading**: Check network tab for API errors
4. **Blank page**: Check browser console for JavaScript errors

### Debug Mode

Set `DEBUG=true` in environment variables to enable verbose logging.

## Support

This is an internal tool for MindRoom operations team. For issues:

1. Check the audit logs for error details
2. Verify all environment variables are set correctly
3. Ensure database migrations are up to date
4. Contact the platform team if issues persist

## License

Private and confidential. For internal MindRoom use only.
