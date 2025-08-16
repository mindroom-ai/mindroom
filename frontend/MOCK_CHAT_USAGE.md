# Mock Chat Screenshots Usage Guide

The mock chat system allows you to programmatically generate realistic chat screenshots for documentation and demos.

## Prerequisites

To generate screenshots, you need Chrome or Chromium installed:

```bash
# On Ubuntu/Debian
sudo apt-get install chromium-browser

# On macOS
brew install --cask chromium

# Or set PUPPETEER_EXECUTABLE_PATH to your Chrome installation
export PUPPETEER_EXECUTABLE_PATH="/usr/bin/chromium"
```

## Generate Screenshots

### Quick Generation

```bash
# Start the dev server
pnpm run dev

# In another terminal, generate screenshots
node generate-readme-screenshots.cjs
```

This will create screenshots in `screenshots/readme/`:

- `chat-matrix.png` - Matrix/Element conversation
- `chat-slack.png` - Slack team discussion
- `chat-discord-collab.png` - Discord cross-org collaboration

### Custom Screenshots

```javascript
const puppeteer = require('puppeteer');

// Define your conversation
const conversation = {
  room: {
    id: 'general',
    name: 'general',
    platform: 'slack',
  },
  users: [
    { id: 'user1', name: 'Alice', color: '#e01e5a' },
    { id: 'bot1', name: 'Assistant', isBot: true },
  ],
  messages: [
    {
      id: 'm1',
      userId: 'user1',
      content: 'Can you help with our API?',
      timestamp: new Date().toISOString(),
    },
  ],
};

// Generate screenshot
async function generate() {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();

  await page.goto('http://localhost:3003/mock-chat');
  await page.evaluate(data => {
    window.mockChatData = data;
  }, conversation);
  await page.reload();

  const chat = await page.$('.mock-chat');
  await chat.screenshot({ path: 'custom-chat.png' });

  await browser.close();
}
```

## Using in README

Once generated, reference the screenshots in your README:

```markdown
## See It In Action

### Monday in Matrix

![Matrix Chat](./frontend/screenshots/readme/chat-matrix.png)

### Tuesday in Slack

![Slack Discussion](./frontend/screenshots/readme/chat-slack.png)

### Cross-Organization Collaboration

![Discord Collaboration](./frontend/screenshots/readme/chat-discord-collab.png)
```

## Platform Styles

Each platform has authentic styling:

- **Matrix/Element**: Clean bubbles, round avatars
- **Slack**: Channel hash, thread indicators, reactions
- **Discord**: Bot badges, hover effects, round avatars
- **Telegram**: Directional bubbles, member count
- **WhatsApp**: Green accents, message tails

## Tips

1. **Realistic timestamps**: Use sequential times for natural flow
2. **Bot indicators**: Set `isBot: true` for AI agents
3. **Reactions**: Add emoji reactions for engagement
4. **User colors**: Assign consistent colors per user
5. **Thread replies**: Use `isThread` and `threadReplies` for discussions

## Troubleshooting

If screenshots fail to generate:

1. **Check Chrome**: Ensure Chrome/Chromium is installed
2. **Dev server**: Verify `http://localhost:3003` is running
3. **Permissions**: May need `--no-sandbox` flag in containers
4. **Wait time**: Increase timeout if styles don't load

## Alternative: React Component

You can also use the MockChat component directly in your app:

```tsx
import { MockChat } from '@/mockChat/MockChat';
import { conversation } from './data';

export function Demo() {
  return <MockChat conversation={conversation} width={800} height={600} />;
}
```

This renders the chat interface without needing screenshots.
