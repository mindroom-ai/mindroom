#!/usr/bin/env node

/**
 * Test script for instance control endpoints (restart, stop, start)
 * Usage: node test-instance-control.js
 */

const API_BASE = 'http://localhost:3000/api/instance';

// Mock instance ID for testing
const MOCK_INSTANCE_ID = 'test-instance-123';

// Test colors
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';
const RESET = '\x1b[0m';

async function testEndpoint(name, endpoint, instanceId) {
  console.log(`${YELLOW}Testing ${name}...${RESET}`);

  try {
    const response = await fetch(`${API_BASE}/${endpoint}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        instanceId: instanceId
      })
    });

    const data = await response.json();

    // 401 Unauthorized is expected and shows the endpoint exists and is secured
    if (response.status === 401 && data.error === 'Unauthorized') {
      console.log(`${GREEN}✓ ${name} endpoint exists and is properly secured${RESET}`);
      return true;
    }

    if (!response.ok) {
      console.log(`${RED}✗ ${name} unexpected error:${RESET}`, data.error);
      return false;
    }

    console.log(`${GREEN}✓ ${name} endpoint exists and responds${RESET}`);
    return true;
  } catch (error) {
    console.log(`${RED}✗ ${name} endpoint not found or crashed:${RESET}`, error.message);
    return false;
  }
}

async function runTests() {
  console.log('\n=== Instance Control API Tests ===\n');
  console.log('Note: These tests check if endpoints exist and respond.');
  console.log('Authentication errors are expected if not logged in.\n');

  const tests = [
    ['Restart', 'restart', MOCK_INSTANCE_ID],
    ['Stop', 'stop', MOCK_INSTANCE_ID],
    ['Start', 'start', MOCK_INSTANCE_ID],
  ];

  let passed = 0;
  let failed = 0;

  for (const [name, endpoint, instanceId] of tests) {
    const result = await testEndpoint(name, endpoint, instanceId);
    if (result) {
      passed++;
    } else {
      failed++;
    }
    console.log('');
  }

  console.log('=== Test Summary ===');
  console.log(`${GREEN}Passed: ${passed}${RESET}`);
  console.log(`${RED}Failed: ${failed}${RESET}`);

  if (failed === 0) {
    console.log(`\n${GREEN}All endpoints are implemented and responding!${RESET}`);
  } else {
    console.log(`\n${YELLOW}Some endpoints need attention.${RESET}`);
  }
}

// Check if the server is running
async function checkServer() {
  try {
    const response = await fetch('http://localhost:3000');
    return response.ok;
  } catch {
    return false;
  }
}

async function main() {
  const serverRunning = await checkServer();

  if (!serverRunning) {
    console.log(`${RED}Error: Customer portal is not running on localhost:3000${RESET}`);
    console.log('Please run: bun run dev');
    process.exit(1);
  }

  await runTests();
}

main().catch(console.error);
