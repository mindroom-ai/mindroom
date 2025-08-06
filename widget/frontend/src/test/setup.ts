import '@testing-library/jest-dom'

// Mock the fetch API
global.fetch = vi.fn()

// Reset mocks before each test
beforeEach(() => {
  vi.clearAllMocks()
})
