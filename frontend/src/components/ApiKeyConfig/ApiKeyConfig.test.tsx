import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { ApiKeyConfig } from './ApiKeyConfig';

// Mock the toast hook
const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock fetch
global.fetch = vi.fn();

describe('ApiKeyConfig', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockClear();
  });

  it('renders with service name and description', () => {
    render(
      <ApiKeyConfig
        service="openai"
        displayName="OpenAI"
        description="Configure your OpenAI API key"
      />
    );

    expect(screen.getByText('OpenAI API Configuration')).toBeInTheDocument();
    expect(screen.getByText('Configure your OpenAI API key')).toBeInTheDocument();
  });

  it('checks for existing API key on mount', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        has_key: true,
        masked_key: 'sk-...abc',
      }),
    });

    render(<ApiKeyConfig service="openai" displayName="OpenAI" />);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith('/api/credentials/openai/api-key?key_name=api_key');
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByText('sk-...abc')).toBeInTheDocument();
    });
  });

  it('shows not configured when no API key exists', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        has_key: false,
      }),
    });

    render(<ApiKeyConfig service="anthropic" displayName="Anthropic" />);

    await waitFor(() => {
      expect(screen.getByText('Not Configured')).toBeInTheDocument();
    });
  });

  it('saves API key when save button is clicked', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: false }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 'success' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: true, masked_key: 'sk-...xyz' }),
      });

    const onConfigured = vi.fn();
    render(<ApiKeyConfig service="openai" displayName="OpenAI" onConfigured={onConfigured} />);

    await waitFor(() => {
      expect(screen.getByText('Not Configured')).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText('Enter API key');
    const saveButton = screen.getByText('Save API Key');

    fireEvent.change(input, { target: { value: 'sk-test-key-123' } });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith('/api/credentials/openai/api-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service: 'openai',
          api_key: 'sk-test-key-123',
          key_name: 'api_key',
        }),
      });
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Success',
        description: 'API key saved for OpenAI',
      });
      expect(onConfigured).toHaveBeenCalled();
    });
  });

  it('shows error when save fails', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: false }),
      })
      .mockResolvedValueOnce({
        ok: false,
      });

    render(<ApiKeyConfig service="openai" displayName="OpenAI" />);

    await waitFor(() => {
      expect(screen.getByText('Not Configured')).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText('Enter API key');
    const saveButton = screen.getByText('Save API Key');

    fireEvent.change(input, { target: { value: 'sk-test-key' } });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Error',
        description: 'Failed to save API key',
        variant: 'destructive',
      });
    });
  });

  it('toggles password visibility', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ has_key: false }),
    });

    render(<ApiKeyConfig service="openai" displayName="OpenAI" />);

    await waitFor(() => {
      expect(screen.getByText('Not Configured')).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText('Enter API key') as HTMLInputElement;
    expect(input.type).toBe('password');

    const toggleButton = screen
      .getByRole('button', { name: '' })
      .parentElement?.querySelector('button[type="button"]');
    if (toggleButton) {
      fireEvent.click(toggleButton);
      expect(input.type).toBe('text');

      fireEvent.click(toggleButton);
      expect(input.type).toBe('password');
    }
  });

  it('deletes API key when delete button is clicked', async () => {
    window.confirm = vi.fn(() => true);

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: true, masked_key: 'sk-...abc' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 'success' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: false }),
      });

    const onConfigured = vi.fn();
    render(<ApiKeyConfig service="openai" displayName="OpenAI" onConfigured={onConfigured} />);

    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
    });

    const deleteButton = screen.getByText('Delete');
    fireEvent.click(deleteButton);

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith(
        'Are you sure you want to delete the API key for OpenAI?'
      );
      expect(global.fetch).toHaveBeenCalledWith('/api/credentials/openai', {
        method: 'DELETE',
      });
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Success',
        description: 'API key deleted for OpenAI',
      });
      expect(onConfigured).toHaveBeenCalled();
    });
  });

  it('tests API key when test button is clicked', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ has_key: true, masked_key: 'sk-...abc' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 'success' }),
      });

    render(<ApiKeyConfig service="openai" displayName="OpenAI" />);

    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
    });

    const testButton = screen.getByText('Test');
    fireEvent.click(testButton);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith('/api/credentials/openai/test', {
        method: 'POST',
      });
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Success',
        description: 'API key is valid',
      });
    });
  });

  it('uses custom key name when provided', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ has_key: false }),
    });

    render(<ApiKeyConfig service="custom" displayName="Custom Service" keyName="custom_token" />);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/credentials/custom/api-key?key_name=custom_token'
      );
    });
  });
});
