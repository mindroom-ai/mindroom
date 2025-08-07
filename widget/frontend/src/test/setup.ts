import '@testing-library/jest-dom';
import { vi, beforeEach, beforeAll } from 'vitest';

// Mock the fetch API
global.fetch = vi.fn();

// Mock scrollIntoView which is not available in jsdom
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

// Reset mocks before each test
beforeEach(() => {
  vi.clearAllMocks();
});
