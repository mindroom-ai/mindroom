import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { GmailToolConfig } from './GmailToolConfig';

// Mock the toast hook
const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock window.open
const mockWindowOpen = vi.fn();

describe('GmailToolConfig', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    global.window.open = mockWindowOpen;
    mockWindowOpen.mockReturnValue({ closed: false });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Automatic Setup (Google OAuth)', () => {
    it('renders the automatic setup tab by default', () => {
      render(<GmailToolConfig />);

      expect(screen.getByText('Gmail Tool Configuration')).toBeInTheDocument();
      expect(screen.getByText('Automatic Setup')).toBeInTheDocument();
      expect(screen.getByText('Setup with Google')).toBeInTheDocument();
    });

    it('clicking "Setup with Google" button triggers OAuth flow', async () => {
      // Mock the fetch response for OAuth start
      global.fetch = vi.fn().mockImplementation(url => {
        if (url.includes('/api/gmail/oauth/start')) {
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({ auth_url: 'https://accounts.google.com/oauth/authorize?...' }),
          });
        }
        if (url.includes('/api/gmail/status')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ configured: false, hasCredentials: false }),
          });
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      });

      render(<GmailToolConfig />);

      const setupButton = screen.getByRole('button', { name: /Setup with Google/i });
      expect(setupButton).toBeEnabled();

      // Click the button
      await userEvent.click(setupButton);

      // Wait for the OAuth flow to start
      await waitFor(() => {
        // Check that fetch was called with correct endpoint
        expect(global.fetch).toHaveBeenCalledWith(
          expect.stringContaining('/api/gmail/oauth/start'),
          expect.objectContaining({
            method: 'POST',
          })
        );

        // Check that window.open was called with the auth URL
        expect(mockWindowOpen).toHaveBeenCalledWith(
          'https://accounts.google.com/oauth/authorize?...',
          '_blank',
          'width=500,height=600'
        );
      });
    });

    it('shows loading state while OAuth is in progress', async () => {
      // Mock the fetch response
      global.fetch = vi.fn().mockImplementation(url => {
        if (url.includes('/api/gmail/oauth/start')) {
          return Promise.resolve({
            ok: true,
            json: () =>
              Promise.resolve({ auth_url: 'https://accounts.google.com/oauth/authorize' }),
          });
        }
        if (url.includes('/api/gmail/status')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ configured: false, hasCredentials: false }),
          });
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      });

      render(<GmailToolConfig />);

      const setupButton = screen.getByRole('button', { name: /Setup with Google/i });

      // Click the button
      await userEvent.click(setupButton);

      // Check for loading state
      await waitFor(() => {
        expect(screen.getByText('Setting up...')).toBeInTheDocument();
      });
    });

    it('handles OAuth errors gracefully', async () => {
      // Mock fetch to return an error
      global.fetch = vi.fn().mockImplementation(url => {
        if (url.includes('/api/gmail/oauth/start')) {
          return Promise.reject(new Error('Network error'));
        }
        if (url.includes('/api/gmail/status')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ configured: false, hasCredentials: false }),
          });
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      });

      render(<GmailToolConfig />);

      const setupButton = screen.getByRole('button', { name: /Setup with Google/i });
      await userEvent.click(setupButton);

      // Wait for error handling
      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: 'Error',
            description: 'Failed to start OAuth flow',
            variant: 'destructive',
          })
        );
      });
    });
  });

  describe('Manual Setup', () => {
    it('can switch to manual setup tab', async () => {
      render(<GmailToolConfig />);

      const manualTab = screen.getByRole('tab', { name: /Manual Setup/i });
      await userEvent.click(manualTab);

      // Check that manual setup content is visible
      expect(screen.getByText('Use Your Own API Keys')).toBeInTheDocument();
      expect(screen.getByLabelText('Client ID')).toBeInTheDocument();
      expect(screen.getByLabelText('Client Secret')).toBeInTheDocument();
    });

    it('validates required fields in manual setup', async () => {
      // Mock status as not configured
      global.fetch = vi.fn().mockImplementation(url => {
        if (url.includes('/api/gmail/status')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ configured: false, hasCredentials: false }),
          });
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      });

      render(<GmailToolConfig />);

      // Switch to manual tab
      const manualTab = screen.getByRole('tab', { name: /Manual Setup/i });
      await userEvent.click(manualTab);

      const saveButton = screen.getByRole('button', { name: /Save Credentials/i });

      // Button should be disabled when fields are empty
      expect(saveButton).toBeDisabled();

      // Fill only one field
      const clientIdInput = screen.getByLabelText('Client ID');
      await userEvent.type(clientIdInput, 'test-client-id');

      // Button should still be disabled with only one field
      expect(saveButton).toBeDisabled();

      // Fill both fields
      const clientSecretInput = screen.getByLabelText('Client Secret');
      await userEvent.type(clientSecretInput, 'test-secret');

      // Now button should be enabled
      expect(saveButton).toBeEnabled();
    });

    it('saves manual credentials successfully', async () => {
      // Mock successful save
      global.fetch = vi.fn().mockImplementation(url => {
        if (url.includes('/api/gmail/configure')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ success: true }),
          });
        }
        if (url.includes('/api/gmail/status')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ configured: false, hasCredentials: false }),
          });
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      });

      render(<GmailToolConfig />);

      // Switch to manual tab
      const manualTab = screen.getByRole('tab', { name: /Manual Setup/i });
      await userEvent.click(manualTab);

      // Fill in the fields
      const clientIdInput = screen.getByLabelText('Client ID');
      const clientSecretInput = screen.getByLabelText('Client Secret');

      await userEvent.type(clientIdInput, 'test-client-id.apps.googleusercontent.com');
      await userEvent.type(clientSecretInput, 'test-client-secret');

      // Save
      const saveButton = screen.getByRole('button', { name: /Save Credentials/i });
      await userEvent.click(saveButton);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith(
          expect.stringContaining('/api/gmail/configure'),
          expect.objectContaining({
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              client_id: 'test-client-id.apps.googleusercontent.com',
              client_secret: 'test-client-secret',
              method: 'manual',
            }),
          })
        );

        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: 'Success!',
            description: 'Gmail credentials saved. Agents can now use Gmail.',
          })
        );
      });
    });
  });

  describe('Configuration Status', () => {
    it('shows configured status when Gmail is set up', async () => {
      // Mock status as configured
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            configured: true,
            method: 'oauth',
            email: 'user@example.com',
            hasCredentials: true,
          }),
      });

      render(<GmailToolConfig />);

      await waitFor(() => {
        expect(screen.getByText('Gmail Configured')).toBeInTheDocument();
        expect(screen.getByText('Connected account: user@example.com')).toBeInTheDocument();
        expect(screen.getByText('✓ Read and search emails')).toBeInTheDocument();
        expect(screen.getByText('✓ Send emails on your behalf')).toBeInTheDocument();
      });
    });

    it('allows reconfiguration when already configured', async () => {
      // Mock status as configured
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            configured: true,
            method: 'oauth',
            email: 'user@example.com',
            hasCredentials: true,
          }),
      });

      render(<GmailToolConfig />);

      await waitFor(() => {
        expect(screen.getByText('Gmail Configured')).toBeInTheDocument();
      });

      const reconfigureButton = screen.getByRole('button', { name: /Reconfigure/i });
      await userEvent.click(reconfigureButton);

      // Should show setup options again
      expect(screen.getByText('Automatic Setup')).toBeInTheDocument();
      expect(screen.getByText('Manual Setup')).toBeInTheDocument();
    });
  });
});
