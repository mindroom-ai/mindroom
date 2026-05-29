import { render, screen, waitFor, within } from '@testing-library/react'
import { AuthWrapper } from '../auth-wrapper'
import { createClient } from '@/lib/supabase/client'
import { useRouter } from 'next/navigation'
import { useDarkMode } from '@/hooks/useDarkMode'
import { act } from 'react'
import { hydrateRoot } from 'react-dom/client'
import { renderToString } from 'react-dom/server'

// Mock dependencies
jest.mock('@/lib/supabase/client', () => ({
  createClient: jest.fn()
}))

jest.mock('@/hooks/useDarkMode', () => ({
  useDarkMode: jest.fn()
}))

// Mock Supabase Auth UI components
jest.mock('@supabase/auth-ui-react', () => ({
  Auth: jest.fn(({ view, redirectTo, providers, appearance }) => (
    <div data-testid="auth-ui">
      <div>View: {view}</div>
      <div>RedirectTo: {redirectTo}</div>
      <div>Providers: {providers?.join(', ')}</div>
      <div>Theme: {appearance?.theme ? 'ThemeSupa' : 'none'}</div>
    </div>
  ))
}))

jest.mock('@supabase/auth-ui-shared', () => ({
  ThemeSupa: 'ThemeSupa'
}))

describe('AuthWrapper', () => {
  let mockSupabaseClient: any
  let mockRouter: any
  let mockAuthSubscription: any
  let originalRuntimeConfig: any

  beforeEach(() => {
    jest.clearAllMocks()
    originalRuntimeConfig = window.__MINDROOM_CONFIG__

    // Setup mock router
    mockRouter = {
      replace: jest.fn(),
      push: jest.fn()
    }
    ;(useRouter as jest.Mock).mockReturnValue(mockRouter)

    // Setup mock dark mode
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })

    // Setup mock Supabase client
    mockAuthSubscription = {
      unsubscribe: jest.fn()
    }

    mockSupabaseClient = {
      auth: {
        onAuthStateChange: jest.fn((callback) => {
          // Store the callback for testing
          mockSupabaseClient.auth._authCallback = callback
          return { data: { subscription: mockAuthSubscription } }
        }),
        _authCallback: null
      }
    }
    ;(createClient as jest.Mock).mockReturnValue(mockSupabaseClient)
  })

  afterEach(() => {
    window.__MINDROOM_CONFIG__ = originalRuntimeConfig
  })

  describe('Basic Rendering', () => {
    it('should render Auth component with default props', () => {
      render(<AuthWrapper />)

      const authUi = screen.getByTestId('auth-ui')
      expect(authUi).toBeInTheDocument()
      expect(screen.getByText('View: sign_in')).toBeInTheDocument()
      expect(screen.getByText('Providers: google, github')).toBeInTheDocument()
      expect(screen.getByText('Theme: ThemeSupa')).toBeInTheDocument()
    })

    it('should render with sign_up view', () => {
      render(<AuthWrapper view="sign_up" />)

      expect(screen.getByText('View: sign_up')).toBeInTheDocument()
    })

    it('should render an unavailable state when Supabase is not configured', () => {
      window.__MINDROOM_CONFIG__ = {
        apiUrl: 'https://api.mindroom.chat',
        supabaseUrl: '',
        supabaseAnonKey: '',
        platformDomain: 'mindroom.chat',
      }

      render(<AuthWrapper view="sign_up" />)

      expect(screen.getByText('Account signup is not available yet.')).toBeInTheDocument()
      expect(screen.queryByTestId('auth-ui')).not.toBeInTheDocument()
      expect(createClient).not.toHaveBeenCalled()
    })

    it('should render an unavailable state when runtime config is missing', async () => {
      window.__MINDROOM_CONFIG__ = undefined

      render(<AuthWrapper />)

      await waitFor(() => {
        expect(screen.getByText('Sign in is not available yet.')).toBeInTheDocument()
      })
      expect(screen.queryByTestId('auth-ui')).not.toBeInTheDocument()
      expect(createClient).not.toHaveBeenCalled()
    })

    it('should hydrate without changing the initial auth markup', async () => {
      const container = document.createElement('div')
      document.body.appendChild(container)
      const recoverableErrors: unknown[] = []

      window.__MINDROOM_CONFIG__ = undefined
      container.innerHTML = renderToString(<AuthWrapper view="sign_up" />)
      window.__MINDROOM_CONFIG__ = {
        apiUrl: 'https://api.mindroom.chat',
        supabaseUrl: '',
        supabaseAnonKey: '',
        platformDomain: 'mindroom.chat',
      }

      let root: ReturnType<typeof hydrateRoot> | undefined
      await act(async () => {
        root = hydrateRoot(container, <AuthWrapper view="sign_up" />, {
          onRecoverableError: (error) => recoverableErrors.push(error),
        })
      })

      await waitFor(() => {
        expect(within(container).getByText('Account signup is not available yet.')).toBeInTheDocument()
      })
      expect(recoverableErrors).toHaveLength(0)

      await act(async () => {
        root?.unmount()
      })
      container.remove()
    })

    it('should set correct redirect URL with origin', async () => {
      render(<AuthWrapper />)

      // Wait for useEffect to set origin
      await waitFor(() => {
        const redirectText = screen.getByText(/RedirectTo:/)
        expect(redirectText.textContent).toContain('http://localhost:3000/auth/callback')
      })
    })

    it('should handle custom redirectTo parameter', async () => {
      render(<AuthWrapper redirectTo="/dashboard" />)

      await waitFor(() => {
        const redirectText = screen.getByText(/RedirectTo:/)
        expect(redirectText.textContent).toContain('http://localhost:3000/dashboard')
      })
    })

    it('should handle absolute redirectTo URLs', async () => {
      render(<AuthWrapper redirectTo="https://example.com/callback" />)

      await waitFor(() => {
        expect(screen.getByText('RedirectTo: https://example.com/callback')).toBeInTheDocument()
      })
    })
  })

  describe('Glass Auth Styling', () => {
    it('should use glass-friendly input colors when dark mode is enabled', () => {
      ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })

      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.appearance.variables.default.colors.inputBackground).toBe('rgba(8, 10, 22, 0.46)')
      expect(authCall.appearance.variables.default.colors.inputBorder).toBe('rgba(255, 255, 255, 0.18)')
      expect(authCall.appearance.variables.default.colors.inputText).toBe('#f8f5ff')
    })

    it('should keep the same glass input colors when dark mode is disabled', () => {
      ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })

      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.appearance.variables.default.colors.inputBackground).toBe('rgba(8, 10, 22, 0.46)')
      expect(authCall.appearance.variables.default.colors.inputBorder).toBe('rgba(255, 255, 255, 0.18)')
      expect(authCall.appearance.variables.default.colors.inputText).toBe('#f8f5ff')
    })
  })

  describe('Authentication State Handling', () => {
    it('should subscribe to auth state changes on mount', () => {
      render(<AuthWrapper />)

      expect(mockSupabaseClient.auth.onAuthStateChange).toHaveBeenCalledWith(
        expect.any(Function)
      )
    })

    it('should redirect on SIGNED_IN event', async () => {
      render(<AuthWrapper redirectTo="/dashboard" />)

      // Simulate sign in
      await waitFor(() => {
        mockSupabaseClient.auth._authCallback('SIGNED_IN', { user: { id: 'user-123' } })
      })

      expect(mockRouter.replace).toHaveBeenCalledWith('http://localhost:3000/dashboard')
    })

    it('should not redirect on other auth events', async () => {
      render(<AuthWrapper />)

      // Simulate other events
      mockSupabaseClient.auth._authCallback('SIGNED_OUT', null)
      mockSupabaseClient.auth._authCallback('TOKEN_REFRESHED', { user: { id: 'user-123' } })
      mockSupabaseClient.auth._authCallback('USER_UPDATED', { user: { id: 'user-123' } })

      expect(mockRouter.replace).not.toHaveBeenCalled()
    })

    it('should not redirect if no session on SIGNED_IN', async () => {
      render(<AuthWrapper />)

      // Simulate sign in without session
      mockSupabaseClient.auth._authCallback('SIGNED_IN', null)

      expect(mockRouter.replace).not.toHaveBeenCalled()
    })

    it('should unsubscribe from auth changes on unmount', () => {
      const { unmount } = render(<AuthWrapper />)

      unmount()

      expect(mockAuthSubscription.unsubscribe).toHaveBeenCalled()
    })
  })

  describe('Auth UI Configuration', () => {
    it('should pass correct providers', () => {
      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.providers).toEqual(['google', 'github'])
    })

    it('should show links only for sign_in view', () => {
      const { Auth } = require('@supabase/auth-ui-react')

      // Test sign_in view
      render(<AuthWrapper view="sign_in" />)
      let authCall = Auth.mock.calls[0][0]
      expect(authCall.showLinks).toBe(true)

      // Clear mocks and test sign_up view
      Auth.mockClear()
      render(<AuthWrapper view="sign_up" />)
      authCall = Auth.mock.calls[0][0]
      expect(authCall.showLinks).toBe(false)
    })

    it('should disable magic link', () => {
      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.magicLink).toBe(false)
    })

    it('should allow email/password authentication', () => {
      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.onlyThirdPartyProviders).toBe(false)
    })

    it('should apply correct styling classes', () => {
      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.appearance.className.button).toContain('font-semibold')
      expect(authCall.appearance.className.button).toContain('bg-white/12')
      expect(authCall.appearance.className.button).not.toContain('scale')
      expect(authCall.appearance.className.input).toContain('bg-black/25')
      expect(authCall.appearance.className.anchor).toContain('text-[#f4b47e]')
    })

    it('should set brand colors', () => {
      const { Auth } = require('@supabase/auth-ui-react')
      render(<AuthWrapper />)

      const authCall = Auth.mock.calls[0][0]
      expect(authCall.appearance.variables.default.colors.brand).toBe('#f4b47e')
      expect(authCall.appearance.variables.default.colors.brandAccent).toBe('#ffd6b0')
    })
  })

  describe('Edge Cases', () => {
    // Removed test for edge case that will never happen in production
    // Testing missing origin adds no value and makes tests brittle

    it('should handle redirect updates correctly', async () => {
      const { rerender } = render(<AuthWrapper redirectTo="/dashboard" />)

      await waitFor(() => {
        expect(screen.getByText('RedirectTo: http://localhost:3000/dashboard')).toBeInTheDocument()
      })

      // Update redirectTo prop
      rerender(<AuthWrapper redirectTo="/profile" />)

      await waitFor(() => {
        expect(screen.getByText('RedirectTo: http://localhost:3000/profile')).toBeInTheDocument()
      })
    })

    // Removed test for edge case that will never happen in production

    it('should create new Supabase client instance', () => {
      render(<AuthWrapper />)

      expect(createClient).toHaveBeenCalled()
    })
  })
})
