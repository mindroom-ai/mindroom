// Learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom'
import 'whatwg-fetch'

// Mock environment variables
process.env.NEXT_PUBLIC_API_URL = 'http://localhost:8000'
process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://test.supabase.co'
process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = 'test-anon-key'

// Mock fetch globally
global.fetch = jest.fn()

// Mock next/navigation
jest.mock('next/navigation', () => ({
  useRouter: jest.fn(() => ({
    push: jest.fn(),
    replace: jest.fn(),
    prefetch: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
  })),
  useSearchParams: jest.fn(() => ({
    get: jest.fn(),
  })),
  usePathname: jest.fn(() => '/test-path'),
}))

// Mock window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: jest.fn().mockImplementation(query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: jest.fn(), // deprecated
    removeListener: jest.fn(), // deprecated
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  })),
})

// Mock window.location for tests
// Suppress the JSDOM navigation error that doesn't affect test results
const originalConsoleError = console.error
const originalConsoleLog = console.log

// Suppress expected console outputs during tests
console.error = (...args) => {
  // Check if this is the JSDOM navigation error
  const firstArg = args[0]
  if (firstArg && typeof firstArg === 'object' && firstArg.type === 'not implemented') {
    // Suppress JSDOM navigation errors - they don't affect our tests
    return
  }
  if (typeof firstArg === 'string' && firstArg.includes('Not implemented: navigation')) {
    return
  }
  // Suppress expected error outputs from tests
  if (typeof firstArg === 'string' && (
    firstArg.includes('Provision error:') ||
    firstArg.includes('API call failed:') ||
    firstArg.includes('Failed to') && firstArg.includes('instance:')
  )) {
    return
  }
  originalConsoleError.call(console, ...args)
}

console.log = (...args) => {
  // Suppress expected log outputs from tests
  const firstArg = args[0]
  if (typeof firstArg === 'string' && (
    firstArg.includes('Provision result:') ||
    firstArg.includes('Request cancelled:')
  )) {
    return
  }
  originalConsoleLog.call(console, ...args)
}

// JSDOM location is read-only, so we delete and replace it
delete window.location
window.location = {
  origin: 'http://localhost:3000',
  href: 'http://localhost:3000',
  pathname: '/',
  search: '',
  hash: '',
  protocol: 'http:',
  hostname: 'localhost',
  host: 'localhost:3000',
  port: '3000',
  reload: jest.fn(),
  replace: jest.fn(),
  assign: jest.fn(),
}
