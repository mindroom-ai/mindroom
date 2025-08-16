const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

// Define mock conversations directly with the component
const mockChats = {
  matrix: {
    platform: 'matrix',
    title: 'MindRoom Development',
    messages: [
      { user: 'You', text: '@assistant Remember our project uses Python 3.11 and FastAPI', time: '10:00 AM' },
      { user: 'mindroom_assistant', text: "Got it! I'll remember that your project uses Python 3.11 and FastAPI.", time: '10:00 AM', isBot: true }
    ]
  },
  slack: {
    platform: 'slack',
    title: '#general',
    messages: [
      { user: 'Sarah Chen', text: 'What Python version are we using?', time: '2:30 PM' },
      { user: 'You', text: '@assistant can you help?', time: '2:30 PM' },
      { user: 'mindroom_assistant', text: "[Joins from Matrix] We're using Python 3.11 with FastAPI", time: '2:31 PM', isBot: true }
    ]
  },
  discord: {
    platform: 'discord',
    title: '#project-review',
    messages: [
      { user: 'Alex (Client)', text: 'Can our architect AI review this with your team?', time: '10:00 AM' },
      { user: 'You', text: 'Sure! @assistant please collaborate with them', time: '10:00 AM' },
      { user: 'mindroom_assistant', text: '[Joins from your Matrix server] Ready to review the architecture.', time: '10:01 AM', isBot: true },
      { user: 'client_architect_ai', text: "[Joins from client's server] I'll share our microservices patterns.", time: '10:01 AM', isBot: true }
    ]
  }
};

// Create HTML template for each platform
function createMockChatHTML(chat) {
  const platformStyles = {
    matrix: {
      bg: '#f3f8fd',
      header: '#fff',
      msgBg: '#e7e7e7',
      sentBg: '#0dbd8b',
      textColor: '#2e2f32'
    },
    slack: {
      bg: '#1a1d21',
      header: '#121317',
      msgBg: 'transparent',
      sentBg: 'transparent',
      textColor: '#d1d2d3'
    },
    discord: {
      bg: '#36393f',
      header: '#2f3136',
      msgBg: 'transparent',
      sentBg: 'transparent',
      textColor: '#dcddde'
    }
  };

  const style = platformStyles[chat.platform] || platformStyles.matrix;

  const messagesHTML = chat.messages.map(msg => `
    <div style="margin-bottom: 16px; ${msg.user === 'You' ? 'text-align: right;' : ''}">
      <div style="display: inline-block; max-width: 70%;">
        <div style="font-weight: 600; margin-bottom: 4px; font-size: 14px; color: ${msg.isBot ? '#5865f2' : style.textColor};">
          ${msg.user}${msg.isBot ? ' <span style="background: #5865f2; color: white; padding: 2px 4px; border-radius: 3px; font-size: 10px; margin-left: 4px;">BOT</span>' : ''}
        </div>
        <div style="background: ${msg.user === 'You' ? style.sentBg : style.msgBg}; ${msg.user === 'You' && chat.platform === 'matrix' ? 'color: white;' : ''} padding: 8px 12px; border-radius: 8px; font-size: 15px;">
          ${msg.text}
        </div>
        <div style="font-size: 11px; opacity: 0.6; margin-top: 4px;">${msg.time}</div>
      </div>
    </div>
  `).join('');

  return `
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
      </style>
    </head>
    <body>
      <div style="width: 800px; height: 600px; background: ${style.bg}; color: ${style.textColor}; display: flex; flex-direction: column;">
        <div style="background: ${style.header}; padding: 16px 20px; border-bottom: 1px solid rgba(0,0,0,0.1);">
          <h2 style="font-size: 16px; font-weight: 600;">
            ${chat.platform === 'slack' ? '#' : ''}${chat.title}
          </h2>
        </div>
        <div style="flex: 1; padding: 20px; overflow-y: auto;">
          ${messagesHTML}
        </div>
        <div style="padding: 16px 20px; border-top: 1px solid rgba(0,0,0,0.1);">
          <input type="text" placeholder="Type a message..." style="width: 100%; padding: 10px; border: 1px solid rgba(0,0,0,0.1); border-radius: 6px; background: rgba(0,0,0,0.05);" disabled />
        </div>
      </div>
    </body>
    </html>
  `;
}

async function generateScreenshot(browser, chatKey, chat) {
  const page = await browser.newPage();

  // Set viewport
  await page.setViewport({ width: 820, height: 620 });

  // Set the HTML content directly
  await page.setContent(createMockChatHTML(chat));

  // Wait for rendering
  await new Promise(resolve => setTimeout(resolve, 500));

  // Take screenshot
  const outputPath = path.join(__dirname, 'screenshots', 'readme', `chat-${chatKey}.png`);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });

  await page.screenshot({
    path: outputPath,
    clip: {
      x: 0,
      y: 0,
      width: 800,
      height: 600
    }
  });

  console.log(`✅ Generated: ${outputPath}`);
  await page.close();
}

async function main() {
  console.log('🎬 Generating mock chat screenshots...');

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  try {
    for (const [key, chat] of Object.entries(mockChats)) {
      await generateScreenshot(browser, key, chat);
    }
    console.log('🎉 All screenshots generated successfully!');
  } catch (error) {
    console.error('❌ Error:', error);
  } finally {
    await browser.close();
  }
}

main();
