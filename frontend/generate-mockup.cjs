const puppeteer = require('puppeteer');
const fs = require('fs').promises;
const path = require('path');

async function generateMockup() {
  console.log('🎨 Generating chat mockup...');

  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  try {
    const page = await browser.newPage();

    // Set viewport for a nice screenshot - smaller to reduce background
    await page.setViewport({
      width: 720,
      height: 520,
      deviceScaleFactor: 2 // High quality
    });

    // Navigate to the HTML file using file:// protocol
    const htmlPath = path.resolve(__dirname, 'chat-mockup.html');
    await page.goto(`file://${htmlPath}`, { waitUntil: 'networkidle0' });

    // Wait for animations
    await new Promise(resolve => setTimeout(resolve, 1000));

    // Take screenshot
    const outputPath = path.join(__dirname, 'chat-mockup.png');
    await page.screenshot({
      path: outputPath,
      fullPage: true
    });

    console.log(`✅ Mockup saved to: ${outputPath}`);
  } catch (error) {
    console.error('❌ Error:', error);
  } finally {
    await browser.close();
  }
}

generateMockup();
