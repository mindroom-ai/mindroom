// Run only at image build time, after the locked Playwright browser install.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const executable = chromium.executablePath();
fs.accessSync(executable, fs.constants.X_OK);
fs.symlinkSync(path.relative(__dirname, executable), path.join(__dirname, 'chromium'));
const registry = JSON.parse(fs.readFileSync(path.join(path.dirname(require.resolve('playwright-core/package.json')), 'browsers.json')));
const browser = registry.browsers.find(browser => browser.name === 'chromium');
fs.writeFileSync(path.join(__dirname, 'browser-version.json'), JSON.stringify({
  server: require('@playwright/mcp/package.json').version,
  playwright: require('playwright/package.json').version,
  revision: browser.revision,
  browserVersion: browser.browserVersion,
  executable,
}, null, 2) + '\n');
