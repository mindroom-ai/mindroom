# Testing Guide for Mindroom Widget

This document provides comprehensive information about testing the Mindroom Widget, including unit tests, integration tests, and end-to-end tests.

## Overview

The widget testing strategy consists of three layers:

- **Unit Tests**: Component-level testing with Jest and React Testing Library
- **Integration Tests**: API and component integration testing
- **E2E Tests**: Full user workflow testing with Playwright

## Quick Start

### Running All Tests

```bash
# Unit tests
pnpm test

# E2E tests (requires backend and frontend running)
pnpm test:e2e

# Everything in one go
pnpm test:all
```

## E2E Integration Tests

### What We Test

The E2E tests validate the complete user workflow for tool configuration:

✅ **Configuration Flow**

- User can search for tools
- Configuration dialog opens correctly
- Credentials are saved and persisted
- UI updates to show "Connected" status

✅ **Disconnection Flow**

- User can disconnect configured tools
- Credentials are properly removed
- UI reverts to "Configure" button

✅ **Form Validation**

- Required fields are enforced
- Invalid inputs show error messages
- Form cannot be submitted with invalid data

✅ **Multi-Field Forms**

- Complex tools like Email with SMTP settings
- All fields are properly saved
- Partial configuration is handled correctly

✅ **Persistence**

- Configuration survives page reload
- Backend and frontend stay in sync
- No data loss on navigation

✅ **Search and Filtering**

- Search functionality works correctly
- Results update dynamically
- Categories filter as expected

### Prerequisites

1. **Install Dependencies**

   ```bash
   cd widget/frontend
   pnpm install
   pnpm playwright:install
   ```

2. **Start Backend** (Terminal 1)

   ```bash
   cd widget/backend
   source ../../.venv/bin/activate
   PYTHONPATH=. python src/main.py
   ```

3. **Start Frontend** (Terminal 2)
   ```bash
   cd widget/frontend
   pnpm dev
   ```

### Running E2E Tests

#### Command Line Options

```bash
# Run all tests in headless mode
pnpm test:e2e

# Watch the browser while tests run
pnpm test:e2e:headed

# Debug tests interactively
pnpm test:e2e:debug

# Use Playwright's UI mode
pnpm test:e2e:ui

# Run specific test
pnpm test:e2e -g "configure Telegram"

# Run in a specific browser
pnpm test:e2e --project=chromium
pnpm test:e2e --project=firefox
pnpm test:e2e --project=webkit

# Generate test report
pnpm test:e2e --reporter=html
```

#### Using the Test Runner Script

```bash
# Basic run
./tests/e2e/run-tests.sh

# With options
./tests/e2e/run-tests.sh --headed
./tests/e2e/run-tests.sh --debug
```

#### Using the Example Runner

```bash
# Check setup and see available commands
node tests/e2e/example-test-runner.cjs
```

### Test Structure

```
tests/e2e/
├── helpers/                    # Page objects and utilities
│   ├── api.helper.ts          # API interactions
│   ├── config-dialog.helper.ts # Configuration dialog
│   └── integrations.helper.ts  # Main integrations page
├── integrations.spec.ts        # Main test suite
├── run-tests.sh               # Bash runner script
├── example-test-runner.cjs    # Node.js runner with checks
└── README.md                  # E2E test documentation
```

### Writing New E2E Tests

#### 1. Use Page Object Pattern

Create helpers for reusable interactions:

```typescript
// helpers/my-feature.helper.ts
export class MyFeatureHelper {
  constructor(private page: Page) {}

  async doSomething() {
    await this.page.click('[data-testid="my-button"]');
  }
}
```

#### 2. Follow Test Structure

```typescript
test.describe('Feature Name', () => {
  let integrationsPage: IntegrationsHelper;
  let apiHelper: ApiHelper;

  test.beforeEach(async ({ page }) => {
    // Setup
    integrationsPage = new IntegrationsHelper(page);
    apiHelper = new ApiHelper(page);

    // Clean state
    await apiHelper.clearAllCredentials();

    // Navigate
    await page.goto('/');
  });

  test('should do something', async () => {
    // Arrange
    await integrationsPage.searchFor('Tool');

    // Act
    await integrationsPage.clickConfigureButton('Tool');

    // Assert
    await expect(page.locator('.status')).toContainText('Connected');
  });
});
```

#### 3. Best Practices

- **Clean State**: Always start with a clean state
- **Wait Properly**: Use `waitFor` instead of fixed timeouts
- **Test Data**: Use test-specific data to avoid conflicts
- **Assertions**: Verify both UI and API state
- **Error Messages**: Make assertions descriptive

### Debugging Failed Tests

#### Local Debugging

1. **Screenshots on Failure**

   ```bash
   # Screenshots are saved to test-results/
   ls test-results/
   ```

2. **Trace Viewer**

   ```bash
   # Run with trace
   pnpm test:e2e --trace on

   # View trace
   npx playwright show-trace trace.zip
   ```

3. **Step-by-Step Debugging**

   ```bash
   # Pause at each step
   pnpm test:e2e:debug
   ```

4. **Browser DevTools**
   ```typescript
   // Add in test
   await page.pause(); // Opens DevTools
   ```

#### CI Debugging

1. **Download Artifacts**

   - Go to GitHub Actions run
   - Download playwright-report artifact
   - Open locally with `npx playwright show-report`

2. **View Screenshots**
   - Failed tests automatically capture screenshots
   - Available in playwright-screenshots artifact

### CI/CD Integration

Tests run automatically on:

- Push to `main` or `develop` branches
- Pull requests to `main`
- When widget files change

See `.github/workflows/e2e-tests.yml` for configuration.

#### Running Tests in CI

The CI workflow:

1. Sets up Python and Node.js environments
2. Installs all dependencies
3. Installs Playwright browsers
4. Runs tests in headless mode
5. Uploads reports and screenshots as artifacts

### Performance Considerations

- Tests run in parallel by default (3 workers)
- Each test file runs in isolation
- Use `test.describe.serial()` for dependent tests
- Typical run time: 2-3 minutes for full suite

### Common Issues and Solutions

#### Backend Not Running

```bash
# Check if backend is running
curl http://localhost:8765/api/health

# If not, start it:
cd widget/backend
PYTHONPATH=. python src/main.py
```

#### Frontend Not Running

```bash
# Check if frontend is running
curl http://localhost:3003

# If not, start it:
cd widget/frontend
pnpm dev
```

#### Playwright Not Installed

```bash
# Install browsers
pnpm playwright:install

# Or manually
npx playwright install chromium
```

#### Port Conflicts

```bash
# Check what's using ports
lsof -i :8765  # Backend
lsof -i :3003  # Frontend

# Kill if needed
kill -9 <PID>
```

#### Timeout Errors

- Increase timeout in `playwright.config.ts`
- Check if services are responding
- Ensure stable network connection

## Unit Tests

### Running Unit Tests

```bash
# Run all unit tests
pnpm test

# Watch mode
pnpm test:watch

# Coverage report
pnpm test:coverage
```

### Writing Unit Tests

```typescript
// MyComponent.test.tsx
import { render, screen } from '@testing-library/react';
import { MyComponent } from './MyComponent';

describe('MyComponent', () => {
  it('renders correctly', () => {
    render(<MyComponent />);
    expect(screen.getByText('Expected Text')).toBeInTheDocument();
  });
});
```

## Test Strategy

### When to Use Each Type

**Unit Tests**:

- Individual component behavior
- Utility functions
- State management logic
- Fast feedback during development

**Integration Tests**:

- API endpoint testing
- Component interactions
- Data flow validation
- Cross-component features

**E2E Tests**:

- Critical user workflows
- Multi-step processes
- Full system validation
- Release confidence

### Coverage Goals

- Unit Tests: 80% coverage
- Integration Tests: Critical paths covered
- E2E Tests: Happy paths + key edge cases

## Continuous Improvement

### Adding New Tests

When adding features:

1. Write unit tests for new components
2. Add integration tests for API changes
3. Create E2E tests for user workflows
4. Update this documentation

### Test Maintenance

- Review failing tests weekly
- Update tests when UI changes
- Remove obsolete tests
- Keep helpers up to date

## Resources

- [Playwright Documentation](https://playwright.dev)
- [Testing Library](https://testing-library.com)
- [Jest Documentation](https://jestjs.io)
- [React Testing Best Practices](https://kentcdodds.com/blog/common-mistakes-with-react-testing-library)
