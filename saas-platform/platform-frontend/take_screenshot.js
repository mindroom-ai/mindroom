#!/usr/bin/env node
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

let PORT = parseInt(process.env.PORT || '3000', 10);
let BASE_URL = `http://localhost:${PORT}`;
const SCREENSHOTS_DIR = path.join(__dirname, 'screenshots');

// Ensure screenshots directory exists
if (!fs.existsSync(SCREENSHOTS_DIR)) {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
}

// Routes to screenshot with their configs
const ROUTES = [
  { path: '/', name: 'landing', viewport: { width: 1920, height: 1080 } },
  { path: '/', name: 'landing-mobile', viewport: { width: 375, height: 812 } },
  { path: '/auth/login', name: 'login', viewport: { width: 1920, height: 1080 } },
  { path: '/auth/signup', name: 'signup', viewport: { width: 1920, height: 1080 } },
  // Dashboard pages require auth - skip them for now
  // Pricing is already on the landing page - no need for separate page
];

// Function to check if server is running
async function isServerRunning(url) {
  try {
    const response = await fetch(url);
    return response.ok;
  } catch {
    return false;
  }
}

// Function to start the dev server
function startDevServer() {
  return new Promise((resolve, reject) => {
    console.log('Starting Next.js dev server...');
    const devServer = spawn('bun', ['run', 'dev'], {
      cwd: __dirname,
      detached: false,
      stdio: 'pipe'
    });

    let serverStarted = false;
    const portRegexes = [
      /Local:\s*http:\/\/localhost:(\d+)/i,
      /started server on [^:]+:(\d+)/i,
      /http:\/\/localhost:(\d+)/i,
    ];

    devServer.stdout.on('data', (data) => {
      const output = data.toString();
      console.log(output);
      for (const rx of portRegexes) {
        const m = output.match(rx);
        if (m && m[1]) {
          const detected = parseInt(m[1], 10);
          if (!Number.isNaN(detected) && detected !== PORT) {
            PORT = detected;
            BASE_URL = `http://localhost:${PORT}`;
            console.log(`Detected Next.js on port ${PORT}. BASE_URL -> ${BASE_URL}`);
          }
          break;
        }
      }
      if (output.includes('Ready in') || output.includes('started server on')) {
        serverStarted = true;
        setTimeout(() => resolve(devServer), 2000); // Give it a bit more time to fully start
      }
    });

    devServer.stderr.on('data', (data) => {
      console.error(`Dev server error: ${data}`);
    });

    devServer.on('error', (error) => {
      reject(error);
    });

    // Timeout if server doesn't start
    setTimeout(() => {
      if (!serverStarted) {
        devServer.kill();
        reject(new Error('Dev server failed to start within 30 seconds'));
      }
    }, 30000);
  });
}

// Main screenshot function
async function takeScreenshots() {
  let devServer = null;
  let browser = null;

  try {
    // Check if server is already running
    const serverRunning = await isServerRunning(BASE_URL);

    if (!serverRunning) {
      console.log(`Server not running on port ${PORT}, starting it...`);
      devServer = await startDevServer();
    } else {
      console.log(`Server already running on port ${PORT}`);
    }

    // Launch browser
    console.log('Launching browser...');
    const launchOptions = {
      headless: 'new',
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--single-process',
        '--no-zygote'
      ]
    };

    // Use Chromium from environment or let Puppeteer download its own
    if (process.env.PUPPETEER_EXECUTABLE_PATH) {
      launchOptions.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
      console.log(`Using Chromium at: ${launchOptions.executablePath}`);
    }

    browser = await puppeteer.launch(launchOptions);

    const page = await browser.newPage();

    // Add timestamp to filenames
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);

    // Helper to scroll to bottom to trigger IntersectionObserver animations
    async function autoScrollPage() {
      await page.evaluate(async () => {
        await new Promise((resolve) => {
          let totalHeight = 0;
          const distance = 600;
          const timer = setInterval(() => {
            window.scrollBy(0, distance);
            totalHeight += distance;
            if (totalHeight >= document.body.scrollHeight - window.innerHeight) {
              clearInterval(timer);
              resolve();
            }
          }, 150);
        });
      });
    }

    // Take screenshots for each route
    for (const route of ROUTES) {
      try {
        console.log(`Taking screenshot of ${route.path} (${route.name})...`);

        // Set viewport
        await page.setViewport(route.viewport);

        // Navigate to page
        await page.goto(`${BASE_URL}${route.path}`, {
          waitUntil: 'networkidle2',
          timeout: 30000
        });

        // Scroll through page to trigger lazy animations
        await autoScrollPage();
        // Brief settle time
        await new Promise(resolve => setTimeout(resolve, 400));

        // Take screenshot
        const filename = `${route.name}_${timestamp}.png`;
        const filepath = path.join(SCREENSHOTS_DIR, filename);

        await page.screenshot({
          path: filepath,
          fullPage: true
        });

        console.log(`✓ Screenshot saved: ${filepath}`);
      } catch (error) {
        console.error(`✗ Failed to screenshot ${route.path}: ${error.message}`);
      }
    }

    console.log('\n✅ All screenshots taken successfully!');
    console.log(`📁 Screenshots saved in: ${SCREENSHOTS_DIR}`);

  } catch (error) {
    console.error('Error:', error);
    process.exit(1);
  } finally {
    // Cleanup
    if (browser) {
      await browser.close();
    }

    if (devServer) {
      console.log('\nStopping dev server...');
      devServer.kill();
    }
  }
}

// Handle script termination
process.on('SIGINT', () => {
  console.log('\nShutting down...');
  process.exit(0);
});

// Run the script
takeScreenshots();
