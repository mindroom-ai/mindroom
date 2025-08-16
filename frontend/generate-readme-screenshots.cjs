const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

// Import the examples
const examples = {
  matrix: {
    room: {
      id: 'mindroom-main',
      name: 'MindRoom Development',
      platform: 'matrix',
      icon: '💬'
    },
    users: [
      { id: 'user1', name: 'You', color: '#0dbd8b' },
      { id: 'assistant', name: 'mindroom_assistant', isBot: true, color: '#5865f2' }
    ],
    messages: [
      {
        id: 'm1',
        userId: 'user1',
        content: '@assistant Remember our project uses Python 3.11 and FastAPI',
        timestamp: '2024-01-15T10:00:00',
        isThread: false
      },
      {
        id: 'm2',
        userId: 'assistant',
        content: "Got it! I'll remember that your project uses Python 3.11 and FastAPI. This will help me provide more accurate assistance with compatible code examples and dependencies.",
        timestamp: '2024-01-15T10:00:15',
        isThread: false
      }
    ]
  },

  slack: {
    room: { id: 'general', name: 'general', platform: 'slack' },
    users: [
      { id: 'colleague', name: 'Sarah Chen', color: '#e01e5a' },
      { id: 'user1', name: 'You', color: '#2eb67d' },
      { id: 'assistant', name: 'mindroom_assistant', isBot: true, color: '#5865f2' }
    ],
    messages: [
      {
        id: 's1',
        userId: 'colleague',
        content: 'What Python version are we using?',
        timestamp: '2024-01-16T14:30:00',
        isThread: false
      },
      {
        id: 's2',
        userId: 'user1',
        content: '@assistant can you help?',
        timestamp: '2024-01-16T14:30:30',
        isThread: false
      },
      {
        id: 's3',
        userId: 'assistant',
        content: "[Joins from Matrix] We're using Python 3.11 with FastAPI",
        timestamp: '2024-01-16T14:30:45',
        isThread: false,
        reactions: [{ emoji: '✅', users: ['colleague', 'user1'] }]
      }
    ]
  },

  discord: {
    room: {
      id: 'project-review',
      name: 'project-review',
      platform: 'discord',
      description: 'Architecture review channel'
    },
    users: [
      { id: 'client', name: 'Alex (Client)', color: '#7289da' },
      { id: 'user1', name: 'You', color: '#43b581' },
      { id: 'assistant', name: 'mindroom_assistant', isBot: true, color: '#5865f2' },
      { id: 'architect', name: 'client_architect_ai', isBot: true, color: '#e91e63' }
    ],
    messages: [
      {
        id: 'd1',
        userId: 'client',
        content: 'Can our architect AI review this with your team?',
        timestamp: '2024-01-18T10:00:00',
        isThread: false
      },
      {
        id: 'd2',
        userId: 'user1',
        content: 'Sure! @assistant please collaborate with them',
        timestamp: '2024-01-18T10:00:30',
        isThread: false
      },
      {
        id: 'd3',
        userId: 'assistant',
        content: '[Joins from your Matrix server] Ready to review the architecture. I have context about our FastAPI patterns and Python 3.11 requirements.',
        timestamp: '2024-01-18T10:01:00',
        isThread: false
      },
      {
        id: 'd4',
        userId: 'architect',
        content: "[Joins from client's server] Excellent. I'll share our microservices patterns and scaling requirements.",
        timestamp: '2024-01-18T10:01:30',
        isThread: false
      }
    ]
  }
};

async function generateScreenshot(browser, conversation, filename) {
  const page = await browser.newPage();

  // Log console messages
  page.on('console', msg => console.log('Browser console:', msg.text()));
  page.on('pageerror', error => console.log('Browser error:', error.message));

  // Set viewport
  await page.setViewport({ width: 1200, height: 800 });

  // Navigate to mock chat page
  await page.goto('http://localhost:3003/mock-chat', { waitUntil: 'networkidle0' });

  // Inject conversation data
  await page.evaluate((data) => {
    window.mockChatData = data;
  }, conversation);

  // Reload to render with data
  await page.reload({ waitUntil: 'networkidle0' });

  // Wait a bit for React to render
  await new Promise(resolve => setTimeout(resolve, 2000));

  // Try to wait for the element, but continue if not found
  try {
    await page.waitForSelector('.mock-chat', { timeout: 3000 });
  } catch (e) {
    console.log('Warning: .mock-chat element not found, trying alternative approach');
    // Try to find any rendered content
    await page.waitForSelector('#root', { timeout: 5000 });
  }

  // Wait a bit for styles
  await new Promise(resolve => setTimeout(resolve, 500));

  // Take screenshot of just the chat element
  const outputPath = path.join(__dirname, 'screenshots', 'readme', `${filename}.png`);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });

  const chatElement = await page.$('.mock-chat');
  if (chatElement) {
    await chatElement.screenshot({ path: outputPath });
    console.log(`✅ Generated: ${outputPath}`);
  } else {
    // Fallback: take a screenshot of the visible content
    await page.screenshot({
      path: outputPath,
      clip: {
        x: 100,
        y: 100,
        width: 800,
        height: 600
      }
    });
    console.log(`⚠️ Generated fallback screenshot: ${outputPath}`);
  }

  await page.close();
}

async function main() {
  console.log('🎬 Generating README screenshots...');

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  try {
    // Generate main examples
    await generateScreenshot(browser, examples.matrix, 'chat-matrix');
    await generateScreenshot(browser, examples.slack, 'chat-slack');
    await generateScreenshot(browser, examples.discord, 'chat-discord-collab');

    console.log('🎉 All screenshots generated successfully!');
  } catch (error) {
    console.error('❌ Error:', error);
  } finally {
    await browser.close();
  }
}

main();
