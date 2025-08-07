# Unified MindRoom Widget Integration

This document provides solutions for integrating the MindRoom configuration widget as a unified experience within Matrix/Element, suitable for production deployment to customers.

## The Challenge

- Element Web (app.element.io) doesn't support custom widgets
- Element Desktop requires an integration manager or manual widget addition
- Separate browser tabs are not acceptable for production deployments
- We need a unified, seamless experience for customers

## Solution 1: Using the /addwidget Command (Immediate Solution)

Element supports adding widgets directly via the `/addwidget` command, which bypasses the need for an integration manager.

### Setup Instructions

1. **Deploy your widget to a public URL**:
   ```bash
   # Example: https://config.mindroom.example.com
   ```

2. **In any Matrix room, use the command**:
   ```
   /addwidget https://config.mindroom.example.com/matrix-widget.html
   ```

3. **The widget will appear in the room** and can be:
   - Pinned for permanent visibility
   - Resized as needed
   - Accessed by all room members

### Customizing the Widget Name

By default, widgets added via `/addwidget` are called "Custom Widget". To rename:

1. Click on the room settings (gear icon)
2. Go to "Developer Tools" → "Explore room state"
3. Find `im.vector.modular.widgets`
4. Select your `customwidget_*` entry
5. Edit the JSON and change the `name` field from "Custom" to "MindRoom Config"
6. Save the changes

## Solution 2: Bot-Assisted Widget Addition (Automated Solution)

Create a MindRoom bot that automatically adds the widget to rooms when invited.

### Implementation

Add this functionality to your MindRoom bot:

```python
import asyncio
from nio import AsyncClient, RoomMessageText, InviteEvent

class MindRoomBot:
    async def add_widget_to_room(self, room_id: str):
        """Add MindRoom config widget to a room via state event"""

        widget_state_event = {
            "type": "im.vector.modular.widgets",
            "state_key": "mindroom_config",
            "content": {
                "type": "custom",
                "url": "https://config.mindroom.example.com/matrix-widget.html",
                "name": "MindRoom Configuration",
                "data": {
                    "title": "MindRoom Configuration",
                    "curl": "https://config.mindroom.example.com"
                }
            }
        }

        await self.client.room_put_state(
            room_id=room_id,
            event_type="im.vector.modular.widgets",
            state_key="mindroom_config",
            content=widget_state_event["content"]
        )

        # Send a message confirming widget addition
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "✅ MindRoom Configuration widget has been added to this room. Pin it for easy access!"
            }
        )

    async def on_room_invite(self, room: InviteEvent):
        """When bot is invited to a room, automatically add the widget"""
        await self.client.join(room.room_id)
        await self.add_widget_to_room(room.room_id)
```

### Bot Commands

Add these commands to your existing MindRoom bot:

- `/mindroom add-widget` - Manually add the configuration widget
- `/mindroom remove-widget` - Remove the configuration widget
- `/mindroom widget-url [url]` - Update the widget URL

## Solution 3: Self-Hosted Integration Manager (Production Solution)

For the best customer experience, deploy your own integration manager using Dimension.

### Dimension Setup

1. **Deploy Dimension** (https://github.com/turt2live/matrix-dimension):
   ```bash
   git clone https://github.com/turt2live/matrix-dimension
   cd matrix-dimension
   npm install
   npm run build
   ```

2. **Configure Dimension** with your widgets:
   ```yaml
   # config.yaml
   widgets:
     - id: "mindroom_config"
       name: "MindRoom Configuration"
       description: "Configure MindRoom agents and settings"
       icon: "https://mindroom.example.com/icon.png"
       url: "https://config.mindroom.example.com/matrix-widget.html"
       categories: ["admin", "configuration"]
   ```

3. **Configure Element** to use your Dimension instance:
   ```json
   {
     "integrations_ui_url": "https://dimension.mindroom.example.com/element",
     "integrations_rest_url": "https://dimension.mindroom.example.com/api/v1/scalar",
     "integrations_widgets_urls": [
       "https://dimension.mindroom.example.com/widgets"
     ]
   }
   ```

## Solution 4: Embed in MindRoom Web Client (Future Solution)

Build your own Matrix web client with the configuration UI built-in.

### Architecture

```
mindroom-web-client/
├── src/
│   ├── matrix-client/     # Matrix SDK integration
│   ├── config-ui/          # Configuration interface
│   └── app.tsx             # Main application
```

This provides:
- Fully integrated configuration UI
- No widget limitations
- Complete control over user experience
- Custom branding and features

## Production Deployment Checklist

### For Customer Deployment

1. **Widget Hosting**:
   - [ ] Deploy widget to HTTPS URL with SSL certificate
   - [ ] Configure CORS to allow Matrix domains
   - [ ] Set up CDN for global availability
   - [ ] Implement authentication if needed

2. **Bot Deployment**:
   - [ ] Deploy MindRoom bot with widget management commands
   - [ ] Configure bot to auto-add widget when invited to rooms
   - [ ] Add permissions system for widget management

3. **Documentation**:
   - [ ] Create customer-facing setup guide
   - [ ] Document the `/addwidget` command
   - [ ] Provide troubleshooting steps

4. **Security**:
   - [ ] Implement CSP headers for widget
   - [ ] Add authentication tokens if needed
   - [ ] Validate Matrix user permissions

## Customer Instructions Template

```markdown
# Adding MindRoom Configuration to Your Room

## Quick Setup (30 seconds)

1. In your Matrix room, type:
   /addwidget https://config.mindroom.your-domain.com

2. Click "Pin widget" to keep it visible

3. Start configuring your MindRoom agents!

## Alternative: Invite the Setup Bot

1. Invite @mindroom_setup:your-domain.com to your room
2. The configuration widget will be added automatically
3. Type /mindroom help for additional options
```

## Technical Implementation Details

### Widget State Event Format

```json
{
  "type": "im.vector.modular.widgets",
  "state_key": "mindroom_config_v1",
  "content": {
    "type": "custom",
    "url": "https://config.mindroom.example.com/matrix-widget.html",
    "name": "MindRoom Configuration",
    "data": {
      "title": "MindRoom Configuration",
      "version": "1.0.0"
    },
    "creatorUserId": "@admin:example.com",
    "id": "mindroom_config_v1"
  }
}
```

### Widget Communication

The widget can communicate with the Matrix room using postMessage:

```javascript
// In your widget code
window.parent.postMessage({
  api: "fromWidget",
  action: "send_event",
  room_id: roomId,
  type: "m.room.message",
  content: {
    msgtype: "m.text",
    body: "Configuration updated successfully!"
  }
}, "*");
```

## Recommended Approach for Production

For immediate deployment to customers, we recommend:

1. **Phase 1**: Use the `/addwidget` command approach
   - Minimal setup required
   - Works in Element Desktop and self-hosted Element Web
   - Document the command clearly for customers

2. **Phase 2**: Deploy a bot for automated widget management
   - Better user experience
   - Automatic widget addition
   - Centralized control

3. **Phase 3**: Consider Dimension or custom client for enterprise customers
   - Full integration manager features
   - Custom branding
   - Advanced widget management

## Testing Your Integration

1. **Local Testing**:
   ```bash
   # Start your widget locally
   ./widget/run.sh

   # In Element Desktop or self-hosted Element Web:
   /addwidget http://localhost:3001/matrix-widget.html
   ```

2. **Production Testing**:
   ```bash
   # Deploy to staging environment
   /addwidget https://staging.config.mindroom.example.com
   ```

## Support and Troubleshooting

### Common Issues

1. **"Widget not loading"**:
   - Check CORS headers
   - Verify HTTPS certificate
   - Check browser console for errors

2. **"Cannot add widget"**:
   - Ensure you have permission in the room
   - Check if widgets are enabled in Element config
   - Try Element Desktop instead of Element Web

3. **"Widget not persisting"**:
   - Check room state events
   - Verify state_key is unique
   - Ensure proper permissions

### Getting Help

- Matrix Widget Development: #matrix-widgets:matrix.org
- Element Support: #element-web:matrix.org
- MindRoom Support: [your support channel]
