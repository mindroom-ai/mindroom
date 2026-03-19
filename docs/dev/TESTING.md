# Testing Guide for the MindRoom Dashboard

## Overview

The dashboard includes comprehensive tests for both frontend (TypeScript/React) and backend (Python/FastAPI) components.

## Frontend Tests (TypeScript/React)

### Test Setup

The frontend uses Vitest as the test runner with React Testing Library for component testing.

**Test Files** (non-exhaustive, representative examples):
- `src/store/configStore.test.ts` - Tests for the Zustand store
- `src/components/AgentList/AgentList.test.tsx` - Tests for the AgentList component
- `src/components/AgentEditor/AgentEditor.test.tsx` - Tests for the AgentEditor component
- `src/components/ModelConfig/ModelConfig.test.tsx` - Tests for the ModelConfig component
- `src/components/ToolConfig/ToolConfigDialog.test.tsx` - Tests for the ToolConfigDialog component
- `src/components/Credentials/Credentials.test.tsx` - Tests for the Credentials component
- `src/components/Knowledge/Knowledge.test.tsx` - Tests for the Knowledge component
- `src/components/TeamEditor/TeamEditor.test.tsx` - Tests for the TeamEditor component
- `src/components/VoiceConfig/VoiceConfig.test.tsx` - Tests for the VoiceConfig component
- `src/components/Integrations/Integrations.test.tsx` - Tests for the Integrations component
- `src/types/toolConfig.test.ts` - Tests for tool configuration types

There are 180+ frontend test files covering components, hooks, and utilities.

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

**Test Files** (non-exhaustive, representative examples):
- `tests/api/test_api.py` - Comprehensive API endpoint tests
- `tests/api/test_file_watcher.py` - File watching functionality tests
- `tests/api/test_credentials_api.py` - Credentials API tests
- `tests/api/test_knowledge_api.py` - Knowledge base API tests
- `tests/api/test_schedules_api.py` - Scheduling API tests
- `tests/api/test_skills_api.py` - Skills API tests
- `tests/api/test_sandbox_runner_api.py` - Sandbox runner API tests
- `tests/conftest.py` - Pytest fixtures and configuration

There are 150+ backend test files covering agents, authorization, commands, config, memory, tools, and more.

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
./run-tests.sh
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

Backend tests run via `.github/workflows/pytest.yml` (Python 3.12).
There is no dedicated frontend test workflow yet; frontend tests are run locally with `bun run test`.

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
4. Add integration tests for dashboard-Matrix communication
5. Add mutation testing
