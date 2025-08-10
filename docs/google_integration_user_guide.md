# Google Integration - User Guide

## The Simplest Setup Ever! 🎉

### For Users: Just Click "Sign In with Google"

That's it! No API keys, no developer console, no configuration. Just:

1. Open MindRoom widget
2. Click "Sign in with Google"
3. Choose your Google account
4. Done! ✅

Your MindRoom agents can now access your Gmail, Calendar, and Drive (with your permission).

---

## Three Integration Options

MindRoom offers three ways to connect Google services, from simplest to most flexible:

### Option 1: Google Sign-In (Simplest) ⭐
**Setup Time**: 0 seconds
**What You Get**: Basic authentication, can be extended for Gmail/Calendar access
**Perfect For**: Getting started quickly

### Option 2: MindRoom OAuth App (Recommended)
**Setup Time**: 0 seconds for users (admin sets it up once)
**What You Get**: Full Gmail, Calendar, Drive access
**Perfect For**: Teams and production use

### Option 3: Your Own OAuth App (Advanced)
**Setup Time**: 10 minutes
**What You Get**: Complete control over permissions and branding
**Perfect For**: Developers and custom deployments

---

## How Each Option Works

### Option 1: Google Sign-In (What we just built)
```
You → Click "Sign in with Google" → Done!
```
- Uses Google's public Sign-In infrastructure
- No setup required at all
- Works immediately

### Option 2: MindRoom OAuth App
```
You → Click "Login with Google" → Authorize MindRoom → Done!
```
- MindRoom maintains the OAuth app
- You just authorize it to access your account
- No technical knowledge needed

### Option 3: Your Own OAuth App
```
You → Set up Google Cloud → Create OAuth app → Configure MindRoom → Login
```
- You control everything
- Requires Google Cloud Console access
- Best for developers

---

## What Can Agents Do?

Once connected, your agents can:

### 📧 Gmail
- Read emails (latest, unread, starred)
- Search emails by sender, date, or content
- Send emails on your behalf
- Create draft emails

### 📅 Google Calendar
- View your calendar events
- Create new events
- Update existing events
- Send meeting invitations

### 📁 Google Drive
- Access your files
- Upload new files
- Organize folders
- Share documents

---

## Privacy & Security

### Your Data is Safe
- MindRoom only accesses what you explicitly authorize
- You can revoke access anytime from Google Account settings
- Tokens are stored securely and never shared

### Revoke Access
1. Go to [Google Account Permissions](https://myaccount.google.com/permissions)
2. Find "MindRoom"
3. Click "Remove Access"

---

## Troubleshooting

### "This app isn't verified"
- This appears during development
- Click "Advanced" → "Go to MindRoom (unsafe)"
- Will disappear once MindRoom is verified by Google

### "Sign-In button doesn't appear"
- Refresh the page
- Check that JavaScript is enabled
- Try a different browser

### "Can't access Gmail"
- You may need to grant additional permissions
- Sign out and sign back in
- Check that Gmail API is enabled (for custom apps)

---

## For Administrators

If you're setting up MindRoom for your organization:

1. **Choose your approach**:
   - Quick Start: Use Google Sign-In (Option 1)
   - Production: Set up MindRoom OAuth App (Option 2)
   - Custom: Create your own OAuth App (Option 3)

2. **One-time setup** (for Option 2 or 3):
   - Takes about 5 minutes
   - See [Admin Setup Guide](./google_oauth_setup_admin.md)
   - All users benefit from your setup

3. **Tell your users**:
   - "Just click Sign in with Google"
   - No other instructions needed!

---

## The Magic of Simplicity ✨

Remember when connecting apps required:
- Creating developer accounts
- Generating API keys
- Reading documentation
- Copying credentials
- Debugging OAuth flows

With MindRoom: **Just click "Sign in with Google"**

That's the experience we've built! 🎉
