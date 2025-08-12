# E2E Integration Tests

This directory contains end-to-end tests for the Widget Configuration system using Playwright.

## Features Tested

- ✅ Tool configuration flow (adding credentials)
- ✅ Tool disconnection (removing credentials)
- ✅ Multiple field configurations (e.g., Email with SMTP settings)
- ✅ Form validation
- ✅ Persistence across page reloads
- ✅ Search and filtering functionality
- ✅ Toast notifications
- ✅ UI state updates after configuration changes

## Prerequisites

1. Install dependencies:

   ```bash
   pnpm install
   pnpm playwright:install
   ```

2. Start the backend:

   ```bash
   cd widget/backend
   python src/main.py
   ```

3. Start the frontend (in another terminal):
   ```bash
   cd widget/frontend
   pnpm dev
   ```

## Running Tests

### Quick Start

```bash
# Run all tests
pnpm test:e2e

# Or use the helper script
./tests/e2e/run-tests.sh
```

### Different Modes

```bash
# Run tests with visible browser
pnpm test:e2e:headed

# Debug tests interactively
pnpm test:e2e:debug

# Use Playwright UI
pnpm test:e2e:ui

# Run specific test file
pnpm test:e2e tests/e2e/integrations.spec.ts

# Run tests matching a pattern
pnpm test:e2e -g "Telegram"
```

## Test Structure

```
tests/e2e/
├── helpers/                    # Page objects and utilities
│   ├── api.helper.ts           # API interactions
│   ├── config-dialog.helper.ts # Configuration dialog interactions
│   └── integrations.helper.ts  # Main integrations page
├── integrations.spec.ts        # Main test suite
├── run-tests.sh                # Local test runner script
└── README.md                   # This file
```

## Writing New Tests

1. **Use Page Objects**: Create helpers in `helpers/` directory for reusable interactions
2. **Clean State**: Always clear credentials in `beforeEach` hook
3. **Wait for Elements**: Use proper waiting strategies instead of fixed timeouts
4. **Verify via API**: Double-check UI changes with API calls

Example test:

```typescript
test('should configure a new tool', async () => {
  // Arrange
  await integrationsPage.searchFor('ToolName');

  // Act
  await integrationsPage.clickConfigureButton('ToolName');
  await configDialog.fillField('API Key', 'test-key');
  await configDialog.save();

  // Assert
  await expect(integrationsPage.getIntegrationStatus('ToolName')).toContain('Connected');
  const status = await apiHelper.getCredentialStatus('toolname');
  expect(status.has_credentials).toBeTruthy();
});
```

## Debugging Failed Tests

1. **Screenshots**: Failed tests automatically capture screenshots in `test-results/`
2. **Traces**: Run with `--trace on` to capture execution traces
3. **Headed Mode**: Use `--headed` to see the browser during test execution
4. **Debug Mode**: Use `--debug` to step through tests interactively

## CI/CD Integration

Tests run automatically on:

- Push to `main` or `develop` branches
- Pull requests to `main`

See `.github/workflows/e2e-tests.yml` for CI configuration.

## Troubleshooting

### Tests fail with "Backend not running"

- Ensure the backend is running on port 8765
- Check: `curl http://localhost:8765/api/health`

### Tests fail with "Frontend not running"

- Ensure the frontend is running on port 3003
- The test config will try to start it automatically

### Browser not installed

- Run: `pnpm playwright:install`
- Or: `npx playwright install chromium`

### Timeout errors

- Increase timeout in `playwright.config.ts`
- Check network conditions
- Ensure services are responding quickly
