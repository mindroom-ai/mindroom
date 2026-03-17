import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EnhancedConfigDialog } from './EnhancedConfigDialog';

const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

global.fetch = vi.fn();

describe('EnhancedConfigDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it('loads and saves scoped credentials with explicit execution_scope', async () => {
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          credentials: {
            api_key: 'existing-key',
          },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 'success' }),
      });

    render(
      <EnhancedConfigDialog
        open={true}
        onClose={onClose}
        service="weather"
        displayName="Weather"
        description="Weather integration"
        configFields={[
          {
            name: 'api_key',
            label: 'API Key',
            type: 'password',
            required: true,
          },
        ]}
        onSuccess={onSuccess}
        agentName="code"
        executionScope="shared"
      />
    );

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/credentials/weather?agent_name=code&execution_scope=shared'
      );
    });

    fireEvent.change(document.getElementById('api_key') as HTMLInputElement, {
      target: { value: 'scoped-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/credentials/weather?agent_name=code&execution_scope=shared',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            credentials: {
              api_key: 'scoped-key',
            },
          }),
        }
      );
      expect(onSuccess).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });
});
