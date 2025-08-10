# MindRoom Integrations Guide

MindRoom provides powerful integrations with external services, available in two modes: **Full Integration** (with API keys) and **Simple Mode** (no setup required).

## 🚀 Quick Start - Simple Mode

Simple Mode provides instant access to services without any configuration. Perfect for quick searches and basic queries.

### Available Services (No API Keys Required)

- **Shopping**: Amazon product search, Walmart availability
- **Social**: Browse Reddit (read-only), GitHub public repos
- **Entertainment**: Movie information from Wikipedia
- **Utility**: Weather, news headlines, stock prices
- **Information**: Wikipedia search, YouTube links

### Using Simple Mode

1. Open the MindRoom widget
2. Navigate to the "✨ Simple Mode" tab
3. Enter your search query
4. Click the service you want to query

Example commands for agents:
```
@mindroom_assistant search amazon for "laptop"
@mindroom_assistant browse reddit technology
@mindroom_assistant get weather for "New York"
@mindroom_assistant search github for "machine learning"
```

### Limitations of Simple Mode

- Read-only access for social platforms
- Estimated/mock data for some services
- No authentication means no private data access
- Limited to public information

## 🔌 Full Integrations (API Keys Required)

For full functionality, connect services through the Integrations tab in the widget.

### Supported Services

#### 📧 Gmail
- **Features**: Search emails, read latest/unread messages
- **Setup**: One-click OAuth connection
- **Requirements**: Google account
- **Agent Tool**: `gmail`

#### 🛒 Amazon Shopping
- **Features**: Product search with real-time pricing
- **Setup**: Amazon Product Advertising API credentials
- **Requirements**: API key and access key
- **Agent Tool**: `integrations`

#### 🎬 IMDb
- **Features**: Movie/TV show search, detailed information, ratings
- **Setup**: OMDB API key (free tier available)
- **Requirements**: API key from [OMDb API](http://www.omdbapi.com/apikey.aspx)
- **Agent Tool**: `integrations`

#### 🎵 Spotify
- **Features**: Current playback, top tracks, user preferences
- **Setup**: OAuth connection
- **Requirements**: Spotify Developer app credentials
- **Agent Tool**: `integrations`

#### 🏪 Walmart
- **Features**: Product search, availability check
- **Setup**: Walmart Open API credentials
- **Requirements**: API key
- **Agent Tool**: `integrations`

#### ✈️ Telegram
- **Features**: Send messages via bot
- **Setup**: Bot token from BotFather
- **Requirements**: Telegram bot token
- **Agent Tool**: `integrations`

#### 👥 Facebook
- **Features**: Page information, posts access
- **Setup**: OAuth connection
- **Requirements**: Facebook Developer app
- **Agent Tool**: `integrations`

#### 🤖 Reddit
- **Features**: Search posts, browse subreddits
- **Setup**: OAuth connection
- **Requirements**: Reddit app credentials
- **Agent Tool**: `integrations`

#### 📦 Dropbox
- **Features**: File listing, storage management
- **Setup**: OAuth connection
- **Requirements**: Dropbox account
- **Agent Tool**: `integrations`

#### 🐙 GitHub
- **Features**: Repository search, issue tracking
- **Setup**: OAuth connection (optional for public repos)
- **Requirements**: GitHub account (for private repos)
- **Agent Tool**: `integrations`

## 🔧 Setup Instructions

### OAuth Services (Gmail, Spotify, Reddit, Dropbox, GitHub, Facebook)

1. Open the MindRoom widget
2. Go to the "Integrations" tab
3. Find the service you want to connect
4. Click "Connect"
5. Authorize MindRoom in the popup window
6. The service will automatically be available to agents

### API Key Services (Amazon, IMDb, Walmart, Telegram)

1. Open the MindRoom widget
2. Go to the "Integrations" tab
3. Find the service you want to configure
4. Click "Configure API Key"
5. Enter your API credentials
6. Click "Configure"

## 🤖 Using Integrations with Agents

Once connected, agents can use these integrations automatically:

### Examples

**Gmail:**
```
@mindroom_assistant search my emails for "invoice"
@mindroom_assistant read my latest emails
@mindroom_assistant check unread emails
```

**Shopping:**
```
@mindroom_assistant search amazon for "wireless headphones"
@mindroom_assistant check walmart for "instant pot"
```

**Entertainment:**
```
@mindroom_assistant find movie "Inception" on IMDb
@mindroom_assistant what's playing on my Spotify?
```

**Social:**
```
@mindroom_assistant search reddit for "python tips"
@mindroom_assistant find GitHub repos about "machine learning"
```

## 🔄 Switching Between Modes

### When to Use Simple Mode
- Quick searches without setup
- Testing integration capabilities
- Public information queries
- When API keys are unavailable

### When to Use Full Integration
- Accessing personal data (emails, playlists)
- Need write capabilities (sending messages)
- Require accurate, real-time data
- Working with private repositories or content

## 🔒 Security & Privacy

### Data Storage
- OAuth tokens are stored locally
- API keys are encrypted before storage
- No credentials are sent to MindRoom servers
- All integrations run locally on your machine

### Permissions
- Only requested permissions are used
- You can disconnect services at any time
- Disconnecting removes all stored credentials

## 🐛 Troubleshooting

### Common Issues

**OAuth Connection Failed**
- Check your browser allows popups
- Ensure you're logged into the service
- Try disconnecting and reconnecting

**API Key Not Working**
- Verify the key is correct
- Check API quotas/limits
- Ensure the key has required permissions

**Simple Mode Not Returning Data**
- Some services may be temporarily unavailable
- Public APIs have rate limits
- Try again after a few minutes

### Getting Help

If you encounter issues:
1. Check the service-specific error message
2. Verify your credentials are correct
3. Try Simple Mode as a fallback
4. Report issues on the MindRoom GitHub repository

## 📝 Developer Notes

### Adding New Integrations

To add support for a new service:

1. **Backend**: Add service configuration to `/widget/backend/src/api/integrations.py`
2. **Frontend**: Update `/widget/frontend/src/components/Integrations/Integrations.tsx`
3. **Agent Tool**: Create tool in `/src/mindroom/integrations_tool.py`
4. **Simple Mode**: Add fallback in `/src/mindroom/simple_integrations.py`

### Available Tool Names

Agents can use these tool names:
- `gmail` - Gmail integration
- `integrations` - All other service integrations
- `simple` - Simple mode fallbacks (no API keys)

## 🚀 Future Enhancements

Planned improvements:
- Browser extension for cookie-based auth
- More services (Slack, Discord, LinkedIn)
- Batch operations support
- Webhook integrations
- Real-time notifications
