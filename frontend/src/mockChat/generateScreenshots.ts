#!/usr/bin/env node
import puppeteer, { Browser, Page } from 'puppeteer';
import { Conversation } from './types';
import * as fs from 'fs/promises';
import * as path from 'path';

interface ScreenshotOptions {
  width?: number;
  height?: number;
  outputDir?: string;
  format?: 'png' | 'jpeg';
  quality?: number;
  fullPage?: boolean;
  light?: boolean;
}

export class MockChatScreenshotGenerator {
  private browser: Browser | null = null;
  private page: Page | null = null;
  private serverUrl: string;

  constructor(serverUrl: string = 'http://localhost:3003') {
    this.serverUrl = serverUrl;
  }

  async initialize() {
    this.browser = await puppeteer.launch({
      headless: true,
    });

    this.page = await this.browser.newPage();

    // Set viewport for consistent rendering
    await this.page.setViewport({
      width: 1200,
      height: 800,
    });
  }

  async generateScreenshot(
    conversation: Conversation,
    filename: string,
    options: ScreenshotOptions = {}
  ): Promise<string> {
    if (!this.page) {
      throw new Error('Generator not initialized. Call initialize() first.');
    }

    const {
      width = 800,
      height = 600,
      outputDir = './screenshots',
      format = 'png',
      quality = 90,
      fullPage = false,
      light = false,
    } = options;

    // Ensure output directory exists
    await fs.mkdir(outputDir, { recursive: true });

    // Navigate to mock chat page with data
    const url = `${this.serverUrl}/mock-chat`;

    // Inject conversation data into the page
    await this.page.goto(url);
    await this.page.evaluate((data: any) => {
      (window as any).mockChatData = data;
    }, conversation);

    // Reload to render with data
    await this.page.reload();

    // Wait for render
    await this.page.waitForSelector('.mock-chat', { timeout: 5000 });

    // Apply light theme if requested
    if (light) {
      await this.page.evaluate(() => {
        document.body.classList.add('light-theme');
      });
    }

    // Set chat dimensions
    await this.page.evaluate(
      ({ w, h }: { w: number; h: number }) => {
        const chat = document.querySelector('.mock-chat') as HTMLElement;
        if (chat) {
          chat.style.width = `${w}px`;
          chat.style.height = `${h}px`;
        }
      },
      { w: width, h: height }
    );

    // Wait a bit for styles to apply
    await new Promise(resolve => setTimeout(resolve, 100));

    // Take screenshot
    const outputPath = path.join(
      outputDir,
      `${filename}.${format}`
    ) as `${string}.${typeof format}`;

    if (fullPage) {
      await this.page.screenshot({
        path: outputPath,
        fullPage: true,
        quality: format === 'jpeg' ? quality : undefined,
      });
    } else {
      // Screenshot just the chat element
      const chatElement = await this.page.$('.mock-chat');
      if (chatElement) {
        await chatElement.screenshot({
          path: outputPath,
          quality: format === 'jpeg' ? quality : undefined,
        });
      }
    }

    return outputPath;
  }

  async generateBatch(
    conversations: Array<{ conversation: Conversation; filename: string }>,
    options: ScreenshotOptions = {}
  ): Promise<string[]> {
    const results: string[] = [];

    for (const { conversation, filename } of conversations) {
      const path = await this.generateScreenshot(conversation, filename, options);
      results.push(path);
      console.log(`Generated: ${path}`);
    }

    return results;
  }

  async cleanup() {
    if (this.page) {
      await this.page.close();
    }
    if (this.browser) {
      await this.browser.close();
    }
  }
}

// Utility function for quick screenshot generation
export async function generateMockChatScreenshot(
  conversation: Conversation,
  filename: string,
  options: ScreenshotOptions = {}
): Promise<string> {
  const generator = new MockChatScreenshotGenerator();

  try {
    await generator.initialize();
    const result = await generator.generateScreenshot(conversation, filename, options);
    return result;
  } finally {
    await generator.cleanup();
  }
}

// CLI usage
if (require.main === module) {
  const args = process.argv.slice(2);

  if (args.length < 2) {
    console.log('Usage: npm run mock-chat <conversation-file.json> <output-filename>');
    process.exit(1);
  }

  const [conversationFile, outputFilename] = args;

  (async () => {
    try {
      const conversationData = JSON.parse(await fs.readFile(conversationFile, 'utf-8'));

      const outputPath = await generateMockChatScreenshot(conversationData, outputFilename, {
        outputDir: './screenshots/mock-chats',
      });

      console.log(`Screenshot saved to: ${outputPath}`);
    } catch (error) {
      console.error('Error generating screenshot:', error);
      process.exit(1);
    }
  })();
}
