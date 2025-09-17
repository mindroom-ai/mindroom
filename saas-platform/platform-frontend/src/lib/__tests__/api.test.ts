import {
  apiCall,
  getAccount,
  setupAccount,
  listInstances,
  provisionInstance,
  startInstance,
  stopInstance,
  restartInstance,
  getPricingConfig,
  createCheckoutSession,
  createPortalSession,
  setSsoCookie,
  clearSsoCookie,
  exportUserData,
  requestAccountDeletion,
  cancelAccountDeletion,
  updateConsent
} from '../api'
import { createClient } from '../supabase/client'

// Mock the Supabase client
jest.mock('../supabase/client', () => ({
  createClient: jest.fn()
}))

// Mock console methods to avoid test output noise
const originalConsoleError = console.error
const originalConsoleLog = console.log
beforeAll(() => {
  console.error = jest.fn()
  console.log = jest.fn()
})
afterAll(() => {
  console.error = originalConsoleError
  console.log = originalConsoleLog
})

describe('API Client', () => {
  let mockSupabase: any
  let mockFetch: jest.MockedFunction<typeof fetch>

  beforeEach(() => {
    jest.clearAllMocks()

    // Setup mock Supabase client
    mockSupabase = {
      auth: {
        getSession: jest.fn().mockResolvedValue({
          data: {
            session: {
              access_token: 'test-token-123',
              user: { id: 'user-123' }
            }
          }
        })
      }
    }
    ;(createClient as jest.Mock).mockReturnValue(mockSupabase)

    // Setup fetch mock
    mockFetch = global.fetch as jest.MockedFunction<typeof fetch>
    mockFetch.mockClear()
  })

  describe('apiCall', () => {
    it('should make authenticated API calls with bearer token', async () => {
      const mockResponse = new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      })
      mockFetch.mockResolvedValueOnce(mockResponse)

      const response = await apiCall('/test-endpoint')

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:8000/test-endpoint',
        expect.objectContaining({
          headers: expect.objectContaining({
            'Content-Type': 'application/json',
            'Authorization': 'Bearer test-token-123'
          })
        })
      )
      expect(response).toBe(mockResponse)
    })

    it('should handle unauthenticated requests', async () => {
      mockSupabase.auth.getSession.mockResolvedValueOnce({
        data: { session: null }
      })

      const mockResponse = new Response('{}', { status: 200 })
      mockFetch.mockResolvedValueOnce(mockResponse)

      await apiCall('/public-endpoint')

      expect(mockFetch).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({
            'Authorization': ''
          })
        })
      )
    })

    it('should handle AbortError gracefully', async () => {
      const abortError = new Error('Request aborted')
      abortError.name = 'AbortError'
      mockFetch.mockRejectedValueOnce(abortError)

      await expect(apiCall('/test')).rejects.toThrow(abortError)
      expect(console.log).toHaveBeenCalledWith('Request cancelled: http://localhost:8000/test')
      expect(console.error).not.toHaveBeenCalled()
    })

    it('should log and throw other errors', async () => {
      const networkError = new Error('Network error')
      mockFetch.mockRejectedValueOnce(networkError)

      await expect(apiCall('/test')).rejects.toThrow(networkError)
      expect(console.error).toHaveBeenCalledWith(
        'API call failed: http://localhost:8000/test',
        networkError
      )
    })

    it('should merge custom headers', async () => {
      const mockResponse = new Response('{}', { status: 200 })
      mockFetch.mockResolvedValueOnce(mockResponse)

      await apiCall('/test', {
        headers: { 'X-Custom-Header': 'value' }
      })

      expect(mockFetch).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          headers: expect.objectContaining({
            'X-Custom-Header': 'value',
            'Authorization': 'Bearer test-token-123'
          })
        })
      )
    })
  })

  describe('Account Management', () => {
    describe('getAccount', () => {
      it('should fetch account successfully', async () => {
        const accountData = { id: 'acc-123', email: 'test@example.com' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(accountData), { status: 200 })
        )

        const result = await getAccount()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/account',
          expect.any(Object)
        )
        expect(result).toEqual(accountData)
      })

      it('should throw error on failed response', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Account not found', { status: 404 })
        )

        await expect(getAccount()).rejects.toThrow('Account not found')
      })

      it('should use default error message if response body is empty', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('', { status: 500 })
        )

        await expect(getAccount()).rejects.toThrow('Failed to fetch account')
      })
    })

    describe('setupAccount', () => {
      it('should setup account successfully', async () => {
        const setupData = { account_id: 'new-acc-123', created: true }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(setupData), { status: 200 })
        )

        const result = await setupAccount()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/account/setup',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(setupData)
      })

      it('should handle setup failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Setup failed: quota exceeded', { status: 403 })
        )

        await expect(setupAccount()).rejects.toThrow('Setup failed: quota exceeded')
      })
    })
  })

  describe('Instance Management', () => {
    describe('listInstances', () => {
      it('should list instances successfully', async () => {
        const instances = [
          { id: 1, status: 'running' },
          { id: 2, status: 'stopped' }
        ]
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(instances), { status: 200 })
        )

        const result = await listInstances()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances',
          expect.any(Object)
        )
        expect(result).toEqual(instances)
      })
    })

    describe('provisionInstance', () => {
      it('should provision instance successfully', async () => {
        const provisionData = { instance_id: 3, status: 'provisioning' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(provisionData), { status: 200 })
        )

        const result = await provisionInstance()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances/provision',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(provisionData)
      })

      it('should handle provision failure with error text', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Quota exceeded', { status: 403 })
        )

        await expect(provisionInstance()).rejects.toThrow('Quota exceeded')
      })

      it('should handle connection abort during provisioning', async () => {
        const mockResponse = {
          ok: false,
          text: jest.fn().mockRejectedValue(new Error('Connection aborted'))
        } as any
        mockFetch.mockResolvedValueOnce(mockResponse)

        await expect(provisionInstance()).rejects.toThrow('Failed to provision instance')
      })
    })

    describe('Instance Control', () => {
      it.each([
        ['start', startInstance],
        ['stop', stopInstance],
        ['restart', restartInstance]
      ])('should %s instance successfully', async (action, fn) => {
        const responseData = { success: true, status: `${action}ed` }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(responseData), { status: 200 })
        )

        const result = await fn('123')

        expect(mockFetch).toHaveBeenCalledWith(
          `http://localhost:8000/my/instances/123/${action}`,
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(responseData)
      })

      it('should handle numeric instance IDs', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('{}', { status: 200 })
        )

        await startInstance(456)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances/456/start',
          expect.any(Object)
        )
      })

      it('should handle instance control errors', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Instance not found', { status: 404 })
        )

        await expect(stopInstance('999')).rejects.toThrow('Instance not found')
      })
    })
  })

  describe('Pricing', () => {
    it('should fetch pricing configuration', async () => {
      const pricingData = {
        tiers: [
          { name: 'basic', price: 10 },
          { name: 'pro', price: 50 }
        ]
      }
      mockFetch.mockResolvedValueOnce(
        new Response(JSON.stringify(pricingData), { status: 200 })
      )

      const result = await getPricingConfig()

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:8000/pricing/config',
        expect.any(Object)
      )
      expect(result).toEqual(pricingData)
    })

    it('should handle pricing fetch failure', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Service unavailable', { status: 503 })
      )

      await expect(getPricingConfig()).rejects.toThrow('Service unavailable')
    })
  })

  describe('Stripe Integration', () => {
    describe('createCheckoutSession', () => {
      it('should create checkout session with all parameters', async () => {
        const sessionData = { session_id: 'cs_123', url: 'https://checkout.stripe.com/...' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(sessionData), { status: 200 })
        )

        const result = await createCheckoutSession('pro', 'yearly', 3)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/stripe/checkout',
          expect.objectContaining({
            method: 'POST',
            body: JSON.stringify({
              tier: 'pro',
              billing_cycle: 'yearly',
              quantity: 3
            })
          })
        )
        expect(result).toEqual(sessionData)
      })

      it('should use default values for optional parameters', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('{}', { status: 200 })
        )

        await createCheckoutSession('basic')

        expect(mockFetch).toHaveBeenCalledWith(
          expect.any(String),
          expect.objectContaining({
            body: JSON.stringify({
              tier: 'basic',
              billing_cycle: 'monthly',
              quantity: 1
            })
          })
        )
      })

      it('should handle checkout session creation failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Invalid tier', { status: 400 })
        )

        await expect(createCheckoutSession('invalid')).rejects.toThrow('Invalid tier')
      })
    })

    describe('createPortalSession', () => {
      it('should create portal session successfully', async () => {
        const portalData = { url: 'https://billing.stripe.com/...' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(portalData), { status: 200 })
        )

        const result = await createPortalSession()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/stripe/portal',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(portalData)
      })
    })
  })

  describe('SSO Cookie Management', () => {
    describe('setSsoCookie', () => {
      it('should set SSO cookie with valid session', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('', { status: 200 })
        )

        const result = await setSsoCookie()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/sso-cookie',
          expect.objectContaining({
            method: 'POST',
            credentials: 'include',
            headers: expect.objectContaining({
              'Authorization': 'Bearer test-token-123'
            })
          })
        )
        expect(result).toEqual({ ok: true })
      })

      it('should return failure when no session exists', async () => {
        mockSupabase.auth.getSession.mockResolvedValueOnce({
          data: { session: null }
        })

        const result = await setSsoCookie()

        expect(mockFetch).not.toHaveBeenCalled()
        expect(result).toEqual({ ok: false })
      })

      it('should handle SSO cookie setting failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Forbidden', { status: 403 })
        )

        const result = await setSsoCookie()

        expect(result).toEqual({ ok: false })
      })
    })

    describe('clearSsoCookie', () => {
      it('should clear SSO cookie', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('', { status: 200 })
        )

        await clearSsoCookie()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/sso-cookie',
          expect.objectContaining({
            method: 'DELETE',
            credentials: 'include'
          })
        )
      })

      it('should not throw even if clearing fails', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('', { status: 500 })
        )

        // Should not throw
        await expect(clearSsoCookie()).resolves.toBeUndefined()
      })
    })
  })

  describe('GDPR Endpoints', () => {
    describe('exportUserData', () => {
      it('should export user data successfully', async () => {
        const exportData = {
          export_date: '2025-01-15T00:00:00Z',
          account_id: 'acc-123',
          personal_data: {
            email: 'test@example.com',
            full_name: 'Test User'
          },
          subscriptions: [],
          instances: [],
          data_retention_periods: {
            personal_data: 'Deleted immediately when you close your account'
          }
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(exportData), { status: 200 })
        )

        const result = await exportUserData()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/gdpr/export-data',
          expect.objectContaining({
            headers: expect.objectContaining({
              'Authorization': 'Bearer test-token-123'
            })
          })
        )
        expect(result).toEqual(exportData)
      })

      it('should handle export failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Unauthorized', { status: 401 })
        )

        await expect(exportUserData()).rejects.toThrow('Unauthorized')
      })
    })

    describe('requestAccountDeletion', () => {
      it('should request deletion without confirmation', async () => {
        const response = {
          status: 'confirmation_required',
          message: 'Please confirm deletion by setting confirmation=true',
          warning: 'This action cannot be undone.'
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await requestAccountDeletion(false)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/gdpr/request-deletion',
          expect.objectContaining({
            method: 'POST',
            body: JSON.stringify({ confirmation: false })
          })
        )
        expect(result).toEqual(response)
      })

      it('should request deletion with confirmation', async () => {
        const response = {
          status: 'deletion_scheduled',
          message: 'Your account has been scheduled for deletion',
          grace_period_days: 7,
          deletion_date: 'Account will be permanently deleted after 7 days'
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await requestAccountDeletion(true)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/gdpr/request-deletion',
          expect.objectContaining({
            method: 'POST',
            body: JSON.stringify({ confirmation: true })
          })
        )
        expect(result).toEqual(response)
      })

      it('should handle deletion request failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Server error', { status: 500 })
        )

        await expect(requestAccountDeletion(true)).rejects.toThrow('Server error')
      })
    })

    describe('cancelAccountDeletion', () => {
      it('should cancel deletion successfully', async () => {
        const response = {
          status: 'success',
          message: 'Account deletion has been cancelled',
          account_status: 'active'
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await cancelAccountDeletion()

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/gdpr/cancel-deletion',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(response)
      })

      it('should handle cancellation when no deletion pending', async () => {
        const response = {
          status: 'not_pending',
          message: 'No deletion request found for this account'
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await cancelAccountDeletion()
        expect(result).toEqual(response)
      })

      it('should handle cancellation failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Not authorized', { status: 403 })
        )

        await expect(cancelAccountDeletion()).rejects.toThrow('Not authorized')
      })
    })

    describe('updateConsent', () => {
      it('should update consent preferences successfully', async () => {
        const response = {
          status: 'success',
          consent: {
            marketing: true,
            analytics: false,
            essential: true
          },
          updated_at: '2025-01-15T00:00:00Z'
        }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await updateConsent(true, false)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/gdpr/consent',
          expect.objectContaining({
            method: 'POST',
            body: JSON.stringify({ marketing: true, analytics: false })
          })
        )
        expect(result).toEqual(response)
      })

      it('should handle all consent combinations', async () => {
        const testCases = [
          { marketing: true, analytics: true },
          { marketing: false, analytics: false },
          { marketing: true, analytics: false },
          { marketing: false, analytics: true }
        ]

        for (const { marketing, analytics } of testCases) {
          mockFetch.mockResolvedValueOnce(
            new Response('{}', { status: 200 })
          )

          await updateConsent(marketing, analytics)

          expect(mockFetch).toHaveBeenLastCalledWith(
            'http://localhost:8000/my/gdpr/consent',
            expect.objectContaining({
              method: 'POST',
              body: JSON.stringify({ marketing, analytics })
            })
          )
        }
      })

      it('should handle consent update failure', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Database error', { status: 500 })
        )

        await expect(updateConsent(false, true)).rejects.toThrow('Database error')
      })
    })
  })

  describe('Error Handling Edge Cases', () => {
    it('should handle network failures gracefully', async () => {
      mockFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))

      await expect(getAccount()).rejects.toThrow('Failed to fetch')
    })

    it('should handle malformed JSON responses', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('not json', {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
      )

      await expect(getAccount()).rejects.toThrow()
    })

    it('should handle empty error messages', async () => {
      const emptyError = new Error('')
      emptyError.name = 'UnknownError'
      mockFetch.mockRejectedValueOnce(emptyError)

      await expect(apiCall('/test')).rejects.toThrow(emptyError)
      // When message is empty, it should log as "Request cancelled"
      expect(console.log).toHaveBeenCalledWith('Request cancelled: http://localhost:8000/test')
    })
  })
})
