const puppeteer = require("puppeteer");
const path = require("path");
const fs = require("fs");

async function takeScreenshot() {
  // Use Chromium from Nix if available, otherwise use Puppeteer's bundled version
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;

  const browser = await puppeteer.launch({
    headless: "new",
    executablePath: executablePath || undefined,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-first-run",
      "--no-zygote",
      "--single-process",
      "--disable-extensions"
    ],
  });

  try {
    const page = await browser.newPage();

    // Set viewport to standard desktop size
    await page.setViewport({
      width: 1280,
      height: 800,
      deviceScaleFactor: 2, // For high quality screenshots
    });

    // Navigate to the widget
    const url = process.env.DEMO_URL || "http://localhost:3003";
    console.log(`Navigating to ${url}...`);
    await page.goto(url, { waitUntil: "networkidle0" });

    // Wait for the widget to be visible - use a more generic selector
    await page.waitForSelector("#root", { visible: true, timeout: 10000 });

    // Wait a bit more for everything to render
    await new Promise(resolve => setTimeout(resolve, 2000));

    // Create screenshots directory if it doesn't exist
    const screenshotsDir = path.join(__dirname, "../screenshots");
    if (!fs.existsSync(screenshotsDir)) {
      fs.mkdirSync(screenshotsDir, { recursive: true });
    }

    // Take full page screenshot
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const fullPagePath = path.join(screenshotsDir, `mindroom-config-fullpage-${timestamp}.png`);
    await page.screenshot({
      path: fullPagePath,
      fullPage: true,
    });
    console.log(`Full page screenshot saved to: ${fullPagePath}`);

    // Click on first agent to show details
    const agentButtons = await page.$$('[role="button"]');
    if (agentButtons.length > 0) {
      await agentButtons[0].click();
      await new Promise(resolve => setTimeout(resolve, 1000));

      // Take screenshot with agent selected
      const selectedPath = path.join(screenshotsDir, `mindroom-config-selected-${timestamp}.png`);
      await page.screenshot({
        path: selectedPath,
        fullPage: true,
      });
      console.log(`Selected agent screenshot saved to: ${selectedPath}`);
    }

    // Switch to Models tab
    const tabButtons = await page.$$('button[role="tab"]');
    for (const button of tabButtons) {
      const text = await page.evaluate(el => el.textContent, button);
      if (text.includes("Models")) {
        await button.click();
        await new Promise(resolve => setTimeout(resolve, 1000));

        // Take screenshot of models tab
        const modelsPath = path.join(screenshotsDir, `mindroom-config-models-${timestamp}.png`);
        await page.screenshot({
          path: modelsPath,
          fullPage: true,
        });
        console.log(`Models tab screenshot saved to: ${modelsPath}`);
        break;
      }
    }

    return {
      fullPage: fullPagePath,
      timestamp: timestamp,
    };
  } catch (error) {
    console.error("Error taking screenshot:", error);
    throw error;
  } finally {
    await browser.close();
  }
}

// Run if called directly
if (require.main === module) {
  takeScreenshot()
    .then(() => {
      console.log("Screenshots captured successfully!");
      process.exit(0);
    })
    .catch((error) => {
      console.error("Failed to capture screenshots:", error);
      process.exit(1);
    });
}

module.exports = { takeScreenshot };
