# Testing Guide for MindRoom Configuration Widget

## Overview

The widget includes comprehensive tests for both frontend (TypeScript/React) and backend (Python/FastAPI) components.

## Frontend Tests (TypeScript/React)

### Test Setup

The frontend uses Vitest as the test runner with React Testing Library for component testing.

**Test Files:**
- `src/store/configStore.test.ts` - Tests for the Zustand store
- `src/components/AgentList/AgentList.test.tsx` - Tests for the AgentList component
- `src/components/AgentEditor/AgentEditor.test.tsx` - Tests for the AgentEditor component
- `src/components/ModelConfig/ModelConfig.test.tsx` - Tests for the ModelConfig component
- `src/components/ToolConfig/ToolConfigDialog.test.tsx` - Tests for the ToolConfigDialog component
- `src/types/toolConfig.test.ts` - Tests for tool configuration types

### Running Frontend Tests

```bash
cd frontend

# Run all tests once
bun test

# Run tests in watch mode
bun run test

# Run tests with UI
bun run test:ui

# Run tests with coverage
bun run test:coverage
```

### Writing Frontend Tests

Example test structure:
```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

describe('ComponentName', () => {
  it('should render correctly', () => {
    render(<ComponentName />)
    expect(screen.getByText('Expected text')).toBeInTheDocument()
  })
})
```

## Backend Tests (Python/FastAPI)

### Test Setup

The backend uses pytest with FastAPI's TestClient for API testing.

**Test Files:**
- `tests/test_api.py` - Comprehensive API endpoint tests
- `tests/test_file_watcher.py` - File watching functionality tests
- `tests/conftest.py` - Pytest fixtures and configuration

### Running Backend Tests

```bash
# From project root
source .venv/bin/activate

# Install test dependencies (if not already installed)
uv sync --all-extras

# Run all API tests
python -m pytest tests/api/

# Run with verbose output
python -m pytest tests/api/ -v

# Run specific test file
python -m pytest tests/api/test_api.py

# Run with coverage
python -m pytest tests/api/ --cov=mindroom.api
```

### Writing Backend Tests

Example test structure:
```python
def test_endpoint(test_client: TestClient):
    """Test description."""
    response = test_client.get("/api/endpoint")
    assert response.status_code == 200
    data = response.json()
    assert "expected_key" in data
```

## Running All Tests

Use the convenience script to run both frontend and backend tests:

```bash
./run-ui-tests.sh
```

Or with Nix for all dependencies:
```bash
./run-ui-tests-nix.sh
```

## Test Coverage

### Frontend Coverage
- Store operations (load, save, CRUD)
- Component rendering and interactions
- API integration

### Backend Coverage
- All API endpoints
- Configuration loading/saving
- File watching
- Error handling
- CORS configuration

## Best Practices

1. **Isolation**: Each test should be independent
2. **Mocking**: Mock external dependencies (API calls, file system)
3. **Descriptive Names**: Use clear test names that describe what's being tested
4. **Arrange-Act-Assert**: Follow the AAA pattern in tests
5. **Coverage**: Aim for high test coverage but focus on critical paths

## CI/CD Integration

To integrate tests into CI/CD:

```yaml
# Example GitHub Actions workflow
name: Tests
on: [push, pull_request]

jobs:
  frontend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-node@v3
        with:
          node-version: '20'
      - run: cd frontend && bun install --frozen-lockfile
      - run: cd frontend && bun test

  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install uv
      - run: uv sync --all-extras
      - run: python -m pytest tests/api/
```

## Troubleshooting

### Frontend Test Issues
- Ensure all dependencies are installed: `bun install`
- Clear cache: `rm -rf node_modules/.vite`
- Check for TypeScript errors: `bun run type-check`

### Backend Test Issues
- Ensure virtual environment is activated
- Install test dependencies: `uv sync --all-extras`
- Check for import errors in test files

## Future Improvements

1. Add E2E tests using Playwright
2. Increase test coverage to >80%
3. Add performance tests
4. Add integration tests for widget-Matrix communication
5. Add mutation testing
