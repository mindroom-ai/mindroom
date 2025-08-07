import '@testing-library/jest-dom';
import { vi, beforeEach } from 'vitest';

// Mock the fetch API
global.fetch = vi.fn();

// Reset mocks before each test
beforeEach(() => {
  vi.clearAllMocks();
});
