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

    // Navigate to the dashboard
    const url = process.env.DEMO_URL || "http://localhost:8765";
    console.log(`Navigating to ${url}...`);
    await page.goto(url, { waitUntil: "networkidle0" });

    // Wait for the dashboard to be visible.
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
    const fullPagePath = path.join(screenshotsDir, `mindroom-dashboard-fullpage-${timestamp}.png`);
    await page.screenshot({
      path: fullPagePath,
      fullPage: true,
    });
    console.log(`Full page screenshot saved to: ${fullPagePath}`);

    // Open the Agents route before looking for its selectable cards.
    await page.click('nav[aria-label="Primary navigation"] a[href="/agents"]');
    await page.waitForSelector('section[aria-label="Agents workspace"]', {
      visible: true,
      timeout: 10000,
    });
    const agentButtons = await page.$$(
      'section[aria-label="Agents workspace"] [role="button"][aria-pressed]',
    );
    if (agentButtons.length === 0) {
      throw new Error("Cannot capture Agents: no configured agents are available to select.");
    }
    await agentButtons[0].click();
    await page.waitForSelector(
      'section[aria-label="Agents workspace"] [role="button"][aria-pressed="true"]',
      { visible: true, timeout: 10000 },
    );
    await page.waitForSelector(
      'section[aria-label="Agents workspace"] [aria-busy="false"] #display_name',
      { visible: true, timeout: 10000 },
    );

    const selectedPath = path.join(screenshotsDir, `mindroom-dashboard-agents-${timestamp}.png`);
    await page.screenshot({
      path: selectedPath,
      fullPage: true,
    });
    console.log(`Selected agent screenshot saved to: ${selectedPath}`);

    // Models is a route link, not a tab button.
    await page.click('nav[aria-label="Primary navigation"] a[href="/models"]');
    await page.waitForSelector('section[aria-label="Models workspace"]', {
      visible: true,
      timeout: 10000,
    });
    await page.waitForSelector(
      'section[aria-label="Models workspace"] [aria-busy="false"] [data-testid="models-table-scroll-container"]',
      { visible: true, timeout: 10000 },
    );
    const modelsPath = path.join(screenshotsDir, `mindroom-dashboard-models-${timestamp}.png`);
    await page.screenshot({
      path: modelsPath,
      fullPage: true,
    });
    console.log(`Models screenshot saved to: ${modelsPath}`);

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
