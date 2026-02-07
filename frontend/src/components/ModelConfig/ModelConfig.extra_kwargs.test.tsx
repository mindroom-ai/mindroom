import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelConfig } from './ModelConfig';
import { useConfigStore } from '@/store/configStore';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
}));

describe('ModelConfig - Add Row Behavior', () => {
  const mockStore = {
    config: {
      models: {
        existing: {
          provider: 'openrouter',
          id: 'openai/gpt-4',
        },
      },
      agents: {},
      defaults: {
        num_history_runs: 5,
        markdown: true,
        add_history_to_messages: true,
      },
      router: { model: 'existing' },
    },
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    saveConfig: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();

    const mockedUseConfigStore = useConfigStore as unknown as {
      mockReturnValue: (value: unknown) => void;
    };
    mockedUseConfigStore.mockReturnValue(mockStore);

    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method || 'GET';
      if (method === 'GET') {
        return {
          ok: true,
          json: async () => ({ has_key: false }),
        };
      }
      return {
        ok: true,
        json: async () => ({ status: 'success' }),
      };
    });

    Object.defineProperty(global, 'fetch', {
      value: fetchMock,
      writable: true,
      configurable: true,
    });
  });

  it('shows editable inputs at the top when adding a model', () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));

    expect(screen.getByPlaceholderText('model name')).toBeTruthy();
    expect(screen.getByPlaceholderText('provider model id')).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Add$/ })).toBeTruthy();
  });

  it('requires name and model id before adding', async () => {
    const { toast } = await import('@/components/ui/toaster');

    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));
    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Error',
          description: 'Model name and model ID are required',
          variant: 'destructive',
        })
      );
      expect(mockStore.updateModel).not.toHaveBeenCalled();
    });
  });
});
