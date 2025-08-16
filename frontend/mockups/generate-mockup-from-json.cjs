const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

// Example conversation structure
const exampleConversation = {
  title: "MindRoom Team Chat",
  messages: [
    {
      id: 1,
      user: {
        name: "Sarah Chen",
        avatar: { type: "initial", value: "S", color: "#e91e63" }
      },
      content: "Hey team, what Python version are we using for the new API?",
      timestamp: "2:30 PM"
    },
    {
      id: 2,
      user: {
        name: "You",
        avatar: { type: "initial", value: "Y", color: "#4caf50" },
        isOwn: true
      },
      content: "@assistant can you remind us about our tech stack?",
      timestamp: "2:31 PM",
      mentions: ["assistant"]
    },
    {
      id: 3,
      user: {
        name: "MindRoom Assistant",
        avatar: { type: "image", value: "../avatars/agents/general.png" },
        badge: { text: "AI Agent", type: "primary" }
      },
      content: "Based on our previous conversations, you're using Python 3.11 with FastAPI for the backend. The project also uses PostgreSQL for the database and Redis for caching.",
      timestamp: "2:31 PM"
    },
    {
      id: 4,
      user: {
        name: "Client's Architect AI",
        avatar: { type: "image", value: "../avatars/agents/analyst.png" },
        badge: { text: "External AI", type: "external" }
      },
      content: "Perfect! That aligns well with our microservices architecture. We can share our deployment patterns optimized for FastAPI.",
      timestamp: "2:32 PM"
    },
    {
      id: 5,
      user: {
        name: "Sarah Chen",
        avatar: { type: "initial", value: "S", color: "#e91e63" }
      },
      content: "Amazing! Two AI agents from different organizations collaborating. This is impossible with traditional AI platforms! 🚀",
      timestamp: "2:32 PM"
    }
  ],
  typing: {
    user: {
      name: "John",
      avatar: { type: "initial", value: "J", color: "#ff9800" }
    }
  }
};

function generateHTML(conversation) {
  // Process mentions in content
  const processMentions = (content, mentions = []) => {
    let processed = content;
    mentions.forEach(mention => {
      processed = processed.replace(
        `@${mention}`,
        `<span class="mention">@${mention}</span>`
      );
    });
    return processed;
  };

  // Generate avatar HTML
  const renderAvatar = (avatar) => {
    if (avatar.type === 'image') {
      return `<div class="avatar"><img src="${avatar.value}" alt="Avatar" /></div>`;
    } else {
      return `<div class="avatar" style="background: ${avatar.color};">${avatar.value}</div>`;
    }
  };

  // Generate badge HTML
  const renderBadge = (badge) => {
    if (!badge) return '';
    const badgeClass = badge.type === 'external' ? 'external-badge' : 'bot-badge';
    return `<span class="${badgeClass}">${badge.text}</span>`;
  };

  // Generate messages HTML
  const messagesHTML = conversation.messages.map(msg => `
      <div class="message${msg.user.isOwn ? ' own' : ''}">
        ${renderAvatar(msg.user.avatar)}
        <div class="message-content">
          <div class="message-header">
            <span class="username">${msg.user.name}</span>
            ${renderBadge(msg.user.badge)}
            <span class="timestamp">${msg.timestamp}</span>
          </div>
          <div class="message-text">
            ${processMentions(msg.content, msg.mentions)}
          </div>
        </div>
      </div>`).join('');

  // Generate typing indicator if present
  const typingHTML = conversation.typing ? `
      <div class="typing-indicator">
        ${renderAvatar(conversation.typing.user.avatar)}
        <div class="typing-dots">
          <div class="dot"></div>
          <div class="dot"></div>
          <div class="dot"></div>
        </div>
      </div>` : '';

  return `<!DOCTYPE html>
<html>
<head>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica', sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      padding: 30px;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
    }

    .chat-container {
      width: 700px;
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.2);
      overflow: hidden;
    }

    .chat-header {
      background: linear-gradient(90deg, #667eea, #764ba2);
      color: white;
      padding: 20px 24px;
      font-size: 18px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .chat-messages {
      padding: 24px;
      background: #f7f8fa;
      min-height: 400px;
    }

    .message {
      display: flex;
      gap: 12px;
      margin-bottom: 20px;
      animation: fadeIn 0.3s ease;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .avatar {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 600;
      color: white;
      font-size: 16px;
      overflow: hidden;
      background: #f0f0f0;
    }

    .avatar img {
      width: 100%;
      height: 100%;
      object-fit: cover;
    }

    .message-content {
      flex: 1;
      max-width: 70%;
    }

    .message-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }

    .username {
      font-weight: 600;
      font-size: 14px;
      color: #1a1a1a;
    }

    .bot-badge {
      background: linear-gradient(90deg, #667eea, #764ba2);
      color: white;
      padding: 2px 6px;
      border-radius: 12px;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
    }

    .external-badge {
      background: linear-gradient(90deg, #764ba2, #f093fb);
      color: white;
      padding: 2px 6px;
      border-radius: 12px;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
    }

    .timestamp {
      font-size: 12px;
      color: #8e9297;
      margin-left: auto;
    }

    .message-text {
      background: white;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 15px;
      line-height: 1.5;
      color: #2e3338;
      box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }

    .message.own {
      flex-direction: row-reverse;
    }

    .message.own .message-content {
      align-items: flex-end;
      display: flex;
      flex-direction: column;
    }

    .message.own .message-text {
      background: linear-gradient(90deg, #667eea, #764ba2);
      color: white;
    }

    .message.own .message-header {
      flex-direction: row-reverse;
    }

    .mention {
      background: rgba(255, 255, 255, 0.2);
      padding: 2px 4px;
      border-radius: 4px;
      font-weight: 600;
    }

    .typing-indicator {
      display: flex;
      gap: 12px;
      padding: 8px 0;
      align-items: center;
    }

    .typing-dots {
      display: flex;
      gap: 4px;
      padding: 8px 12px;
      background: white;
      border-radius: 18px;
      align-items: center;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #8e9297;
      animation: typing 1.4s infinite;
    }

    .dot:nth-child(2) { animation-delay: 0.2s; }
    .dot:nth-child(3) { animation-delay: 0.4s; }

    @keyframes typing {
      0%, 60%, 100% { opacity: 0.3; }
      30% { opacity: 1; }
    }
  </style>
</head>
<body>
  <div class="chat-container">
    <div class="chat-header">
      <span>🧠</span>
      <span>${conversation.title}</span>
    </div>

    <div class="chat-messages">
      ${messagesHTML}
      ${typingHTML}
    </div>
  </div>
</body>
</html>`;
}

async function generateMockupFromJSON(conversation = exampleConversation, outputName = 'chat-mockup') {
  console.log('🎨 Generating chat mockup from JSON...');

  const html = generateHTML(conversation);

  // Save HTML file
  const htmlPath = path.join(__dirname, `${outputName}.html`);
  await fs.writeFile(htmlPath, html);
  console.log(`📝 HTML saved to: ${htmlPath}`);

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  try {
    const page = await browser.newPage();

    // Set viewport for a nice screenshot
    await page.setViewport({
      width: 720,
      height: 520,
      deviceScaleFactor: 2 // High quality
    });

    // Navigate to the generated HTML file
    await page.goto(`file://${htmlPath}`, { waitUntil: 'networkidle0' });

    // Wait for animations
    await new Promise(resolve => setTimeout(resolve, 1000));

    // Take screenshot
    const outputPath = path.join(__dirname, `${outputName}.png`);
    await page.screenshot({
      path: outputPath,
      fullPage: true
    });

    console.log(`✅ Screenshot saved to: ${outputPath}`);
  } catch (error) {
    console.error('❌ Error:', error);
  } finally {
    await browser.close();
  }
}

// If running directly, generate the example
if (require.main === module) {
  generateMockupFromJSON();
}

module.exports = { generateMockupFromJSON, exampleConversation };
