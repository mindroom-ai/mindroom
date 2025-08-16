#!/usr/bin/env tsx
import { MockChatScreenshotGenerator } from '../src/mockChat/generateScreenshots';
import { examples } from '../src/mockChat/examples/README-examples';
import * as path from 'path';

async function generateAllExamples() {
  const generator = new MockChatScreenshotGenerator();

  console.log('🎬 Starting mock chat screenshot generation...');

  try {
    await generator.initialize();

    const screenshots = [
      { conversation: examples.matrix, filename: 'matrix-monday' },
      { conversation: examples.slack, filename: 'slack-tuesday' },
      { conversation: examples.discord, filename: 'discord-collab' },
      { conversation: examples.multiAgent, filename: 'multi-agent' },
      { conversation: examples.telegram, filename: 'telegram-client' },
      { conversation: examples.whatsapp, filename: 'whatsapp-team' },
    ];

    console.log(`📸 Generating ${screenshots.length} screenshots...`);

    const results = await generator.generateBatch(screenshots, {
      outputDir: path.join(process.cwd(), 'screenshots', 'mock-chats'),
      width: 800,
      height: 600,
      format: 'png',
    });

    console.log('✅ Screenshots generated successfully:');
    results.forEach(r => console.log(`   - ${r}`));

    // Also generate light theme versions
    console.log('\n🌞 Generating light theme versions...');

    const lightResults = await generator.generateBatch(
      screenshots.map(s => ({
        ...s,
        filename: `${s.filename}-light`,
      })),
      {
        outputDir: path.join(process.cwd(), 'screenshots', 'mock-chats'),
        width: 800,
        height: 600,
        format: 'png',
        light: true,
      }
    );

    console.log('✅ Light theme screenshots generated:');
    lightResults.forEach(r => console.log(`   - ${r}`));
  } catch (error) {
    console.error('❌ Error generating screenshots:', error);
    process.exit(1);
  } finally {
    await generator.cleanup();
  }

  console.log('\n🎉 All mock chat screenshots generated successfully!');
}

// Run if called directly
if (require.main === module) {
  generateAllExamples();
}
