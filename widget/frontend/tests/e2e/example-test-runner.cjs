#!/usr/bin/env node

/**
 * Example test runner that demonstrates how to run the integration tests
 * This can be executed directly: node tests/e2e/example-test-runner.js
 */

const { execSync } = require('child_process');
const path = require('path');

console.log('🧪 Mindroom Widget Integration Tests Example');
console.log('============================================\n');

// Check if services are running
function checkService(name, url) {
  try {
    execSync(`curl -s ${url}`, { stdio: 'ignore' });
    console.log(`✅ ${name} is running at ${url}`);
    return true;
  } catch {
    console.log(`❌ ${name} is not running at ${url}`);
    return false;
  }
}

const backendReady = checkService('Backend', 'http://localhost:8765/api/health');
const frontendReady = checkService('Frontend', 'http://localhost:3003');

if (!backendReady || !frontendReady) {
  console.log('\n⚠️  Please ensure both services are running:');
  if (!backendReady) {
    console.log('\nStart backend:');
    console.log('  cd widget/backend');
    console.log('  source ../../.venv/bin/activate');
    console.log('  PYTHONPATH=. python src/main.py');
  }
  if (!frontendReady) {
    console.log('\nStart frontend:');
    console.log('  cd widget/frontend');
    console.log('  pnpm dev');
  }
  console.log('\nThen run this script again.');
  process.exit(1);
}

console.log('\n📋 Available test commands:\n');

const commands = [
  {
    name: 'Run all tests',
    cmd: 'pnpm test:e2e',
    description: 'Runs all integration tests in headless mode'
  },
  {
    name: 'Run with visible browser',
    cmd: 'pnpm test:e2e:headed',
    description: 'See the browser while tests run'
  },
  {
    name: 'Debug mode',
    cmd: 'pnpm test:e2e:debug',
    description: 'Step through tests interactively'
  },
  {
    name: 'Playwright UI',
    cmd: 'pnpm test:e2e:ui',
    description: 'Use the Playwright test runner UI'
  },
  {
    name: 'Run specific test',
    cmd: 'pnpm test:e2e -g "Telegram"',
    description: 'Run only tests matching "Telegram"'
  },
  {
    name: 'Run single browser',
    cmd: 'pnpm test:e2e --project=chromium',
    description: 'Run tests only in Chromium'
  }
];

commands.forEach(({ name, cmd, description }) => {
  console.log(`📌 ${name}`);
  console.log(`   Command: ${cmd}`);
  console.log(`   ${description}\n`);
});

console.log('💡 Example: Running a single test in headed mode:');
console.log('   npx playwright test --headed -g "configure Telegram"');

console.log('\n🎯 Test coverage:');
console.log('   • Tool configuration (adding credentials)');
console.log('   • Tool disconnection (removing credentials)');
console.log('   • Multi-field forms (Email SMTP settings)');
console.log('   • Form validation');
console.log('   • Persistence across reloads');
console.log('   • Search and filtering');

console.log('\n📊 View test report after running:');
console.log('   npx playwright show-report');

console.log('\n✨ Happy testing!');
