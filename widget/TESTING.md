# Testing Guide for MindRoom Configuration Widget

## Overview

The widget includes comprehensive tests for both frontend (TypeScript/React) and backend (Python/FastAPI) components.

## Frontend Tests (TypeScript/React)

### Test Setup

The frontend uses Vitest as the test runner with React Testing Library for component testing.

**Test Files:**
- `src/store/configStore.test.ts` - Tests for the Zustand store
- `src/components/AgentList/AgentList.test.tsx` - Tests for the AgentList component

### Running Frontend Tests

```bash
cd widget/frontend

# Run all tests once
npm test

# Run tests in watch mode
npm run test

# Run tests with UI
npm run test:ui

# Run tests with coverage
npm run test:coverage
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
cd widget/backend

# Activate virtual environment
source .venv/bin/activate

# Install test dependencies (if not already installed)
uv sync --all-extras

# Run all tests
python -m pytest

# Run with verbose output
python -m pytest -v

# Run specific test file
python -m pytest tests/test_api.py

# Run with coverage
python -m pytest --cov=src
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
cd widget
./run_tests.sh
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
      - run: cd widget/frontend && npm ci
      - run: cd widget/frontend && npm test

  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install uv
      - run: cd widget/backend && uv sync --all-extras
      - run: cd widget/backend && python -m pytest
```

## Troubleshooting

### Frontend Test Issues
- Ensure all dependencies are installed: `npm install`
- Clear cache: `rm -rf node_modules/.vite`
- Check for TypeScript errors: `npm run type-check`

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
