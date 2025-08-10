const puppeteer = require('puppeteer');
const { spawn } = require('child_process');
const path = require('path');

describe('Gmail OAuth Integration E2E Tests', () => {
  let browser;
  let page;
  let backendProcess;
  let frontendProcess;

  // Helper function to wait for servers to start
  const waitForServer = (url, maxAttempts = 30) => {
    return new Promise((resolve, reject) => {
      let attempts = 0;
      const checkServer = async () => {
        try {
          const response = await fetch(url);
          if (response.ok) {
            resolve();
          } else {
            throw new Error('Server not ready');
          }
        } catch (error) {
          attempts++;
          if (attempts >= maxAttempts) {
            reject(new Error(`Server at ${url} did not start in time`));
          } else {
            setTimeout(checkServer, 1000);
          }
        }
      };
      checkServer();
    });
  };

  beforeAll(async () => {
    console.log('Starting backend and frontend servers...');

    // Start backend server
    const backendPath = path.join(__dirname, '..', '..', 'backend');
    backendProcess = spawn('uv', ['run', 'uvicorn', 'src.main:app', '--port', '8001'], {
      cwd: backendPath,
      detached: false,
    });

    backendProcess.stdout.on('data', data => {
      console.log(`Backend: ${data}`);
    });

    backendProcess.stderr.on('data', data => {
      console.error(`Backend Error: ${data}`);
    });

    // Start frontend server
    const frontendPath = path.join(__dirname, '..');
    frontendProcess = spawn('pnpm', ['run', 'dev', '--port', '5173'], {
      cwd: frontendPath,
      detached: false,
      env: { ...process.env, BROWSER: 'none' }, // Prevent auto-opening browser
    });

    frontendProcess.stdout.on('data', data => {
      console.log(`Frontend: ${data}`);
    });

    frontendProcess.stderr.on('data', data => {
      console.error(`Frontend Error: ${data}`);
    });

    // Wait for servers to be ready
    console.log('Waiting for servers to start...');
    await waitForServer('http://localhost:8001/health');
    await waitForServer('http://localhost:5173');
    console.log('Servers are ready!');

    // Launch browser
    browser = await puppeteer.launch({
      headless: false, // Set to true for CI
      slowMo: 50, // Slow down for debugging
      devtools: true,
    });
  }, 60000); // 60 second timeout for setup

  afterAll(async () => {
    // Close browser
    if (browser) {
      await browser.close();
    }

    // Kill server processes
    if (backendProcess) {
      backendProcess.kill('SIGTERM');
    }
    if (frontendProcess) {
      frontendProcess.kill('SIGTERM');
    }

    // Give processes time to clean up
    await new Promise(resolve => setTimeout(resolve, 2000));
  });

  beforeEach(async () => {
    page = await browser.newPage();

    // Set up console log capture
    page.on('console', msg => console.log('Browser Console:', msg.text()));
    page.on('pageerror', error => console.error('Browser Error:', error));

    // Set up request interception to log network activity
    await page.setRequestInterception(true);
    page.on('request', request => {
      console.log('Request:', request.method(), request.url());
      request.continue();
    });

    page.on('response', response => {
      console.log('Response:', response.status(), response.url());
    });
  });

  afterEach(async () => {
    if (page) {
      await page.close();
    }
  });

  test('Gmail Tool Config component loads', async () => {
    // Navigate to the widget
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Wait for the Gmail Tool Config to appear
    await page.waitForSelector('text/Gmail Tool Configuration', { timeout: 10000 });

    // Check that the component rendered
    const title = await page.$eval('h3', el => el.textContent);
    expect(title).toContain('Gmail Tool Configuration');
  }, 30000);

  test('Automatic Setup tab is default', async () => {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Wait for tabs to load
    await page.waitForSelector('[role="tab"]');

    // Check that Automatic Setup tab is selected
    const automaticTab = await page.$('[role="tab"]:first-child');
    const ariaSelected = await automaticTab.evaluate(el => el.getAttribute('aria-selected'));
    expect(ariaSelected).toBe('true');

    // Check that the Setup with Google button is visible
    await page.waitForSelector('button:has-text("Setup with Google")');
  }, 30000);

  test('Clicking "Setup with Google" button triggers OAuth flow', async () => {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Wait for the button to be visible
    await page.waitForSelector('button:has-text("Setup with Google")');

    // Set up window.open interception
    let newWindowUrl = null;
    await page.evaluateOnNewDocument(() => {
      window._originalOpen = window.open;
      window.open = (url, target, features) => {
        window._lastOpenedUrl = url;
        console.log('Window.open called with:', url);
        // Return a mock window object
        return { closed: false };
      };
    });

    // Reload to apply the window.open override
    await page.reload({ waitUntil: 'networkidle0' });
    await page.waitForSelector('button:has-text("Setup with Google")');

    // Click the button
    console.log('Clicking "Setup with Google" button...');
    await page.click('button:has-text("Setup with Google")');

    // Wait a moment for the click to process
    await page.waitForTimeout(2000);

    // Check if window.open was called
    const openedUrl = await page.evaluate(() => window._lastOpenedUrl);
    console.log('Opened URL:', openedUrl);

    // Verify that OAuth endpoint was called
    const requests = await page.evaluate(() => {
      return performance
        .getEntriesByType('resource')
        .filter(entry => entry.name.includes('/api/integrations/gmail/oauth/start'))
        .map(entry => entry.name);
    });

    console.log('OAuth API requests:', requests);
    expect(requests.length).toBeGreaterThan(0);
  }, 30000);

  test('Manual setup tab can be accessed', async () => {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Wait for tabs
    await page.waitForSelector('[role="tab"]');

    // Click on Manual Setup tab
    const manualTab = await page.$('[role="tab"]:has-text("Manual Setup")');
    await manualTab.click();

    // Wait for manual setup content
    await page.waitForSelector('text/Use Your Own API Keys');

    // Check that input fields are present
    await page.waitForSelector('input[id="client-id"]');
    await page.waitForSelector('input[id="client-secret"]');
    await page.waitForSelector('button:has-text("Save Credentials")');
  }, 30000);

  test('Manual setup validates required fields', async () => {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Switch to manual tab
    await page.click('[role="tab"]:has-text("Manual Setup")');
    await page.waitForSelector('text/Use Your Own API Keys');

    // Try to save without filling fields
    await page.click('button:has-text("Save Credentials")');

    // Wait for toast notification (error message)
    await page.waitForTimeout(1000);

    // Check if validation occurred by looking at network requests
    const saveRequests = await page.evaluate(() => {
      return performance
        .getEntriesByType('resource')
        .filter(entry => entry.name.includes('/api/integrations/gmail/configure')).length;
    });

    // Should not have made a request since validation failed
    expect(saveRequests).toBe(0);
  }, 30000);

  test('Manual setup can save credentials', async () => {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0' });

    // Switch to manual tab
    await page.click('[role="tab"]:has-text("Manual Setup")');
    await page.waitForSelector('text/Use Your Own API Keys');

    // Fill in the fields
    await page.type('input[id="client-id"]', 'test-client-id.apps.googleusercontent.com');
    await page.type('input[id="client-secret"]', 'test-client-secret-123');

    // Click save
    await page.click('button:has-text("Save Credentials")');

    // Wait for the API call
    await page.waitForTimeout(2000);

    // Check if save request was made
    const saveRequests = await page.evaluate(() => {
      return performance
        .getEntriesByType('resource')
        .filter(entry => entry.name.includes('/api/integrations/gmail/configure')).length;
    });

    expect(saveRequests).toBeGreaterThan(0);
  }, 30000);
});
