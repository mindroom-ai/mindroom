# MindRoom Widget Integration Guide

## Quick Start

### For Users - Getting the Widget in Your Room

#### Option 1: Use the !widget Command (Simplest)
In any MindRoom room, type:
```
!widget
```

#### Option 2: Use Element's /addwidget Command
In Element Desktop:
```
/addwidget http://localhost:3001/matrix-widget.html
```

#### Option 3: Direct Browser Access
Open http://localhost:3001 in your browser while MindRoom is running.

**⚠️ Important:** Element Web (app.element.io) does NOT support custom widgets. You need Element Desktop or self-hosted Element Web.

## For Developers

### Starting the Widget

```bash
# From the mindroom directory
./widget/run.sh
```

This starts:
- Backend API on port 8001
- Frontend UI on port 3001

### How It Works

1. User types `!widget` in a Matrix room
2. MindRoom bot sends a Matrix state event (`im.vector.modular.widgets`)
3. Element Desktop renders the widget in an iframe
4. Widget syncs with config.yaml in real-time

## Production Deployment

### Hosting the Widget

1. **Build the frontend:**
   ```bash
   cd widget/frontend
   npm run build
   ```

2. **Deploy with nginx:**
   ```nginx
   server {
       listen 443 ssl http2;
       server_name widget.yourdomain.com;

       # Frontend
       location / {
           root /path/to/widget/frontend/dist;
           try_files $uri $uri/ /index.html;
       }

       # Backend API
       location /api {
           proxy_pass http://localhost:8001;
       }
   }
   ```

3. **Configure CORS** in backend for your domain

4. **Users can then use:**
   ```
   !widget https://widget.yourdomain.com
   ```

### Custom Widget URL

Specify a custom URL:
```
!widget https://config.yourdomain.com
```

## Alternative Access Methods

Since Element Web doesn't support widgets, alternatives include:

1. **Element Desktop** - Download from https://element.io/download
2. **Browser Split Screen** - Run widget in separate tab
3. **Desktop App Mode** - `google-chrome --app=http://localhost:3001`
4. **Self-Host Element Web** with widgets enabled

## Troubleshooting

### Widget Not Showing
- Ensure you're using Element Desktop, not Element Web
- Check widget server is running (`./widget/run.sh`)
- Try `/addwidget` command in Element

### Command Not Recognized
- Ensure MindRoom is running
- Check you're in a room with MindRoom agents

### Failed to Add Widget
- Check room permissions
- Review MindRoom logs
- Test in a different room

## Technical Details

### Widget State Event Format
```json
{
  "type": "im.vector.modular.widgets",
  "state_key": "mindroom_config",
  "content": {
    "type": "custom",
    "url": "https://widget.url",
    "name": "MindRoom Configuration"
  }
}
```

### Architecture
```
Matrix Room → State Event → Element Desktop → iframe → Widget Frontend → Backend API → config.yaml
```

## Development

See `widget/README.md` for detailed development instructions including:
- Adding new features
- Testing
- API endpoints
- File structure
