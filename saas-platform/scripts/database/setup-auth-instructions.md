# Supabase Authentication Setup

## Quick Setup Instructions

Since Supabase authentication providers must be configured via the dashboard, follow these steps:

### 1. Access Supabase Dashboard
- Go to: https://supabase.com/dashboard/project/lxcziijbiqaxoavavrco
- Navigate to: Authentication → Providers

### 2. Enable Email Authentication
- Email provider should be enabled by default
- Ensure "Enable Email Signup" is checked

### 3. Configure Google OAuth
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable Google+ API
4. Go to Credentials → Create Credentials → OAuth client ID
5. Configure:
   - Application type: Web application
   - Authorized redirect URIs:
     ```
     https://lxcziijbiqaxoavavrco.supabase.co/auth/v1/callback
     ```
6. Copy Client ID and Client Secret
7. In Supabase Dashboard:
   - Enable Google provider
   - Paste Client ID and Client Secret
   - Save

### 4. Configure GitHub OAuth
1. Go to [GitHub Settings](https://github.com/settings/developers)
2. New OAuth App
3. Configure:
   - Application name: MindRoom Staging
   - Homepage URL: https://app.staging.mindroom.chat
   - Authorization callback URL:
     ```
     https://lxcziijbiqaxoavavrco.supabase.co/auth/v1/callback
     ```
4. Copy Client ID and Client Secret
5. In Supabase Dashboard:
   - Enable GitHub provider
   - Paste Client ID and Client Secret
   - Save

### 5. Configure Redirect URLs
In Supabase Dashboard → Authentication → URL Configuration:

Add these redirect URLs:
```
https://app.staging.mindroom.chat/auth/callback
https://app.mindroom.chat/auth/callback
http://localhost:3000/auth/callback
```

Site URL:
```
https://app.staging.mindroom.chat
```

## Environment Variables

After configuration, add these to your `.env` file:

```bash
# Already configured
SUPABASE_URL=https://lxcziijbiqaxoavavrco.supabase.co
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_SERVICE_KEY=your_service_key

# OAuth (optional - for reference)
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret
```

## Testing

1. Visit: https://app.staging.mindroom.chat/auth/signup
2. Try signing up with:
   - Email/Password
   - Google OAuth
   - GitHub OAuth

## Troubleshooting

- **"Provider not enabled" error**: Enable the provider in Supabase Dashboard
- **Redirect mismatch error**: Ensure redirect URLs match exactly in both OAuth provider and Supabase
- **CORS errors**: Check Site URL configuration in Supabase
