# Matrix/Element Widget Integration

This guide explains how to integrate the MindRoom Configuration widget into your Matrix client.

## Important: Widget Support Limitations

**⚠️ Element Web (app.element.io) does NOT support custom widgets for security reasons.**

Widgets are only available in:
- **Element Desktop** (downloadable application)
- **Self-hosted Element Web** instances with widgets enabled
- **Other Matrix clients** that support widgets (FluffyChat, etc.)

## Prerequisites

1. MindRoom widget running (see widget/README.md for setup)
2. One of:
   - Element Desktop (download from https://element.io/download)
   - Self-hosted Element Web with widget support enabled
   - Another Matrix client with widget support
3. Admin access to the room where you want to add the widget

## Quick Setup

### Method 1: Using Element Desktop

1. **Start the widget server** (if not already running):
   ```bash
   ./widget/run.sh
   # Widget will be available at http://localhost:3001
   ```

2. **Open Element** and navigate to the room where you want to add the widget

3. **Add the widget**:
   - Click the room settings (gear icon)
   - Go to "Widgets" section
   - Click "Add widgets, bridges & bots"
   - Select "Custom Widget"
   - Enter the following:
     - **Widget URL**: `http://localhost:3001/matrix-widget.html?url=http://localhost:3001`
     - **Widget Name**: `MindRoom Config`
     - **Widget Type**: `Custom Widget`
   - Click "Add Widget"

4. **Pin the widget** (optional):
   - Click the three dots on the widget
   - Select "Pin widget" to keep it always visible

### Method 2: Using the Integration Manager

If your homeserver has an integration manager configured:

1. Click the room settings
2. Go to "Add widgets, bridges & bots"
3. Look for "MindRoom Config" (if available)
4. Click "Add"

### Method 3: Using Matrix Widget URL

For remote access or hosted deployment:

```
https://your-domain.com/widget/matrix-widget.html?url=https://your-domain.com/widget
```

## Alternative Access Methods (For Element Web Users)

Since Element Web (app.element.io) doesn't support custom widgets, here are alternative ways to use the MindRoom configuration tool:

### Option 1: Direct Browser Access

Simply open the widget in a separate browser tab:

1. **Start the widget**:
   ```bash
   ./widget/run.sh
   ```

2. **Open in browser**:
   ```
   http://localhost:3001
   ```

3. **Keep it open alongside Element Web** in a separate tab or window

### Option 2: Use Element Desktop

Download and install Element Desktop which has full widget support:

1. **Download Element Desktop** from https://element.io/download
2. **Login** with your Matrix account
3. **Follow the widget setup instructions** above

### Option 3: Browser Split Screen

Use your browser's split-screen or picture-in-picture features:

1. **Open Element Web** in one tab
2. **Open the widget** (http://localhost:3001) in another tab
3. **Use browser extensions** like "Tab Resize" or "Split Screen" to view both simultaneously

### Option 4: Desktop Widget Tools

Use desktop widget tools to create a floating window:

**On macOS:**
- Use "Fluid" or "Nativefier" to create a standalone app from the widget URL

**On Windows:**
- Use "WebView2" or create a shortcut with `--app=http://localhost:3001`

**On Linux:**
- Use `google-chrome --app=http://localhost:3001` to create an app window
- Or use tools like "Nativefier" or "Web Apps" in GNOME

### Option 5: Self-Host Element Web

If you need widget support in a web interface:

1. **Deploy your own Element Web instance**:
   ```bash
   git clone https://github.com/vector-im/element-web.git
   cd element-web
   yarn install
   yarn build
   ```

2. **Configure to enable widgets** in `config.json`:
   ```json
   {
     "integrations_ui_url": "https://scalar.vector.im/",
     "integrations_rest_url": "https://scalar.vector.im/api",
     "integrations_widgets_urls": ["https://scalar.vector.im/api"],
     "enable_widgets": true
   }
   ```

3. **Serve the built files** with a web server

## Widget Features in Matrix

When running as a Matrix widget, the MindRoom configuration tool provides:

- **Real-time sync**: Changes are immediately reflected in the config file
- **Multi-user support**: Multiple users can view the configuration
- **Responsive design**: Adapts to the widget container size
- **Always-on-screen**: Can be pinned to stay visible while chatting

## Configuration Options

### URL Parameters

The widget wrapper (`matrix-widget.html`) accepts the following parameters:

- `url`: The URL of the actual widget application (default: `http://localhost:3001`)
- `theme`: Color theme - `light` or `dark` (default: follows system)

Example:
```
http://localhost:3001/matrix-widget.html?url=http://localhost:3001&theme=dark
```

### Security Considerations

1. **Local Development**:
   - The widget runs on localhost by default
   - Only accessible from your machine
   - No authentication required

2. **Production Deployment**:
   - Use HTTPS for the widget URL
   - Implement authentication if needed
   - Consider CORS settings for cross-origin requests
   - Use a reverse proxy for better security

## Hosting the Widget

### For Personal Use (Local)

The default setup runs the widget locally, which is perfect for personal use:

```bash
# Start both backend and frontend
./widget/run.sh

# Access at http://localhost:3001
```

### For Team Use (Network)

To make the widget accessible on your local network:

1. **Find your local IP**:
   ```bash
   # On Linux/Mac
   ip addr show | grep inet
   # or
   hostname -I

   # On Windows
   ipconfig
   ```

2. **Start the widget with network binding**:
   ```bash
   # Backend
   cd widget/backend
   uv run uvicorn src.main:app --host 0.0.0.0 --port 8001

   # Frontend (in another terminal)
   cd widget/frontend
   npm run dev -- --host 0.0.0.0
   ```

3. **Add to Element using your network IP**:
   - Widget URL: `http://YOUR_IP:3001/matrix-widget.html?url=http://YOUR_IP:3001`

### For Production (Internet)

For production deployment:

1. **Deploy the widget** to a web server (nginx, Apache, etc.)
2. **Configure HTTPS** with proper SSL certificates
3. **Set up reverse proxy** for the backend API
4. **Update CORS settings** in the backend to allow your domain
5. **Add authentication** if needed

Example nginx configuration:

```nginx
server {
    listen 443 ssl http2;
    server_name widget.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # Frontend
    location / {
        root /path/to/widget/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    # Backend API
    location /api {
        proxy_pass http://localhost:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Matrix widget wrapper
    location /matrix-widget.html {
        root /path/to/widget/frontend/public;
    }
}
```

## Troubleshooting

### Widget Not Loading

1. **Check if the widget server is running**:
   ```bash
   curl http://localhost:3001
   # Should return HTML content
   ```

2. **Check browser console** for errors (F12 in most browsers)

3. **Verify CORS settings** if accessing from a different domain

4. **Check Element permissions**:
   - Ensure you have permission to add widgets to the room
   - Try in a test room first

### Widget Not Updating

1. **Check backend connection**:
   ```bash
   curl http://localhost:8001/api/config/agents
   # Should return JSON with agents
   ```

2. **Verify file permissions** on `config.yaml`

3. **Check browser network tab** for failed API requests

### Widget Too Small

- Click and drag the widget borders to resize
- Use the "Pin widget" option for a larger view
- Open in a new tab using the expand button

## Advanced Integration

### Custom Widget Capabilities

The widget can request additional Matrix capabilities:

```javascript
// In the widget code
widgetApi.requestCapability('m.always_on_screen');
widgetApi.requestCapability('m.sticker');
widgetApi.requestCapability('org.matrix.msc2931.navigate');
```

### Sending Updates to Matrix

The widget can send updates back to the Matrix room:

```javascript
// Send a message when configuration changes
widgetApi.sendEvent('m.room.message', {
    msgtype: 'm.text',
    body: 'Configuration updated: Added new agent'
});
```

### Widget State Persistence

The widget state can be saved to Matrix room state:

```javascript
// Save widget state
widgetApi.sendStateEvent('m.widget.state', 'mindroom-config', {
    lastModified: Date.now(),
    configVersion: '1.0.0'
});
```

## Next Steps

1. **Customize the widget** appearance to match your Matrix theme
2. **Add authentication** for production deployments
3. **Implement real-time collaboration** features
4. **Create widget presets** for common configurations

## Support

For issues or questions about the widget integration:

1. Check the main README.md for general setup
2. Review widget/README.md for widget-specific details
3. Check Element's widget documentation
4. Open an issue on the MindRoom repository
