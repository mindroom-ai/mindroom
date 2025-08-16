# Mock Chat Screenshot Generator

A clean, programmatic system for generating mock chat screenshots that look like real chat interfaces from various platforms (Matrix, Slack, Discord, Telegram, WhatsApp).

## Features

- 🎨 **Multi-Platform Support**: Accurate styling for Matrix/Element, Slack, Discord, Telegram, and WhatsApp
- 🌓 **Theme Support**: Both light and dark themes for supported platforms
- 📝 **Type-Safe**: Full TypeScript support with comprehensive types
- 🤖 **Bot Support**: Special rendering for bot users with badges
- 💬 **Rich Messages**: Support for reactions, threads, and edited messages
- 📸 **Automated Screenshots**: Playwright-based screenshot generation
- 🎯 **Clean Architecture**: Modular, extensible design

## Quick Start

### 1. Define a Conversation

```typescript
import { Conversation } from './mockChat/types';

const conversation: Conversation = {
  room: {
    id: 'general',
    name: 'general',
    platform: 'slack',
  },
  users: [
    {
      id: 'user1',
      name: 'Alice',
      color: '#e01e5a',
    },
    {
      id: 'bot1',
      name: 'MindRoom Assistant',
      isBot: true,
      color: '#5865f2',
    },
  ],
  messages: [
    {
      id: 'm1',
      userId: 'user1',
      content: 'Can you help with our API design?',
      timestamp: new Date('2024-01-20T10:00:00'),
    },
    {
      id: 'm2',
      userId: 'bot1',
      content: "I'd be happy to help! Based on your FastAPI setup...",
      timestamp: new Date('2024-01-20T10:00:30'),
      reactions: [{ emoji: '👍', users: ['user1'] }],
    },
  ],
};
```

### 2. Generate a Screenshot

```typescript
import { generateMockChatScreenshot } from './mockChat/generateScreenshots';

// Generate a single screenshot
await generateMockChatScreenshot(conversation, 'api-discussion', {
  width: 800,
  height: 600,
  outputDir: './screenshots',
  format: 'png',
});
```

### 3. Batch Generation

```typescript
import { MockChatScreenshotGenerator } from './mockChat/generateScreenshots';

const generator = new MockChatScreenshotGenerator();
await generator.initialize();

const conversations = [
  { conversation: slackChat, filename: 'slack-demo' },
  { conversation: discordChat, filename: 'discord-demo' },
  { conversation: matrixChat, filename: 'matrix-demo' },
];

await generator.generateBatch(conversations, {
  outputDir: './screenshots/demos',
  width: 800,
  height: 600,
});

await generator.cleanup();
```

## Platform-Specific Features

### Slack

- Channel hash (#) prefix
- Thread indicators with reply counts
- Slack-style reactions
- Hover effects on messages

### Discord

- Round avatars
- Bot badges
- Channel descriptions
- Message hover highlighting

### Matrix/Element

- Message bubbles with sent/received styling
- Round avatars
- Clean, modern design

### Telegram

- Message bubbles with directional styling
- Member count display
- Telegram-specific color scheme

### WhatsApp

- Message bubbles with tail effects
- Read receipts (coming soon)
- WhatsApp green accent colors

## Customization

### Custom Themes

```typescript
import { getTheme } from './mockChat/themes';

// Get light theme variant
const lightSlack = getTheme('slack', true);

// Custom theme colors can be added to themes.ts
```

### Custom User Avatars

```typescript
const user = {
  id: 'user1',
  name: 'John Doe',
  avatar: 'https://example.com/avatar.jpg', // URL to avatar image
  color: '#667eea', // Fallback color for text avatar
};
```

## CLI Usage

Generate screenshots from JSON files:

```bash
# Single screenshot
npm run mock-chat conversations/example.json output-name

# Generate all README examples
npm run generate-mock-screenshots
```

## React Component Usage

Use the MockChat component directly in your React app:

```tsx
import { MockChat } from './mockChat/MockChat';
import { conversation } from './data';

function App() {
  return <MockChat conversation={conversation} width={800} height={600} showHeader={true} />;
}
```

## File Structure

```
mockChat/
├── types.ts                 # TypeScript type definitions
├── MockChat.tsx            # Main React component
├── MockChat.css            # Platform-specific styles
├── MockChatPage.tsx        # Standalone page for screenshots
├── themes.ts               # Platform color themes
├── generateScreenshots.ts  # Playwright screenshot generator
├── examples/               # Example conversations
│   └── README-examples.ts  # Examples from the main README
└── README.md              # This file
```

## Tips

1. **Consistent Timestamps**: Use realistic time intervals between messages
2. **User Colors**: Assign consistent colors to users for better visual distinction
3. **Bot Indicators**: Always set `isBot: true` for AI agents
4. **Reactions**: Add reactions to show engagement
5. **Threads**: Use thread indicators for complex discussions

## Future Enhancements

- [ ] Message status indicators (delivered, read)
- [ ] Typing indicators
- [ ] File attachments
- [ ] Code blocks with syntax highlighting
- [ ] Embedded links with previews
- [ ] Voice message indicators
- [ ] Custom emoji support
