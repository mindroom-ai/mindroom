import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";
import { vi, beforeEach, beforeAll } from "vitest";

// A loaded host can push a correct wait past Testing Library's 1 s default.
// 3 s stays below Vitest's 5 s test timeout, so a wait that fails still reports its own assertion.
configure({ asyncUtilTimeout: 3000 });

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
