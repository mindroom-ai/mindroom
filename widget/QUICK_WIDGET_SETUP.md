# Quick Widget Setup

## For Users

### Option 1: Use the !widget Command (Simplest)

In any MindRoom room, just type:
```
!widget
```

This will add the configuration widget to your room. The widget lets you visually configure agents, models, and settings.

**Note:** You need Element Desktop or self-hosted Element Web. The hosted app.element.io doesn't support widgets.

### Option 2: Use Element's /addwidget Command

In Element Desktop, type:
```
/addwidget http://localhost:3001/matrix-widget.html
```

### Option 3: Direct Browser Access

Open http://localhost:3001 in your browser while MindRoom is running.

## For Developers

### Starting the Widget

```bash
# From the mindroom directory
./widget/run.sh
```

This starts:
- Backend API on port 8001
- Frontend UI on port 3001

### How the !widget Command Works

1. User types `!widget` in a Matrix room
2. MindRoom bot receives the command
3. Bot sends a Matrix state event of type `im.vector.modular.widgets`
4. Element renders the widget in an iframe
5. Widget syncs with config.yaml in real-time

### Custom Widget URL

You can specify a custom URL:
```
!widget https://config.yourdomain.com
```

### Widget Architecture

```
Matrix Room
    ↓
State Event (im.vector.modular.widgets)
    ↓
Element Desktop renders iframe
    ↓
Widget Frontend (React)
    ↓
Widget Backend (FastAPI)
    ↓
config.yaml
```

## Production Deployment

For production, deploy the widget to a public URL:

1. Build the frontend:
   ```bash
   cd widget/frontend
   npm run build
   ```

2. Serve the built files with nginx/Apache

3. Deploy the backend API

4. Users can then use:
   ```
   !widget https://config.yourdomain.com
   ```

## Troubleshooting

### "Widget not showing"
- Make sure you're using Element Desktop, not Element Web
- Check that the widget server is running (`./widget/run.sh`)
- Try the `/addwidget` command in Element

### "Command not recognized"
- Make sure MindRoom is running
- The !widget command only works when the router agent is active
- Check that you're in a room where MindRoom agents are present

### "Failed to add widget"
- Check room permissions (you need permission to modify room state)
- Check the MindRoom logs for errors
- Try in a test room first
