// Learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom'
import 'whatwg-fetch'

const { getServerRuntimeConfig } = require('./src/lib/runtime-config')

// Mock environment variables for runtime config (server side)
process.env.SUPABASE_URL = 'https://test.supabase.co'
process.env.SUPABASE_ANON_KEY = 'test-anon-key'

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
// Only suppress JSDOM navigation errors - they're not helpful for debugging
const originalConsoleError = console.error

console.error = (...args) => {
  // Always suppress JSDOM navigation errors - they don't help with debugging
  const firstArg = args[0]
  if (firstArg && typeof firstArg === 'object' && firstArg.type === 'not implemented') {
    return
  }
  if (typeof firstArg === 'string' && firstArg.includes('Not implemented: navigation')) {
    return
  }
  // Let all other console outputs through - they might be needed for debugging
  originalConsoleError.call(console, ...args)
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

// Inject runtime config expected by browser helpers
window.__MINDROOM_CONFIG__ = getServerRuntimeConfig()
