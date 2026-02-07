import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelConfig } from './ModelConfig';
import { useConfigStore } from '@/store/configStore';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
}));

type KeyStatusResponse = {
  has_key: boolean;
  source?: string;
  masked_key?: string;
  api_key?: string;
};

function extractService(url: string): string {
  const marker = '/api/credentials/';
  const start = url.indexOf(marker);
  if (start === -1) return '';
  const rest = url.slice(start + marker.length);
  const end = rest.indexOf('/api-key');
  return end === -1 ? rest : rest.slice(0, end);
}

describe('ModelConfig', () => {
  const mockStore = {
    config: {
      models: {
        default: { provider: 'ollama', id: 'devstral:24b' },
        anthropic: { provider: 'anthropic', id: 'claude-3-5-haiku-latest' },
        openrouter: { provider: 'openrouter', id: 'z-ai/glm-4.5-air:free' },
        openrouter_backup: { provider: 'openrouter', id: 'openai/gpt-4o-mini' },
      },
      agents: {},
      defaults: { num_history_runs: 5, markdown: true, add_history_to_messages: true },
      router: { model: 'default' },
    },
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue(undefined),
  };

  let keyStatusByService: Record<string, KeyStatusResponse>;
  let fetchMock: ReturnType<typeof vi.fn>;
  const writeTextMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();

    const mockedUseConfigStore = useConfigStore as unknown as {
      mockReturnValue: (value: unknown) => void;
    };
    mockedUseConfigStore.mockReturnValue(mockStore);

    keyStatusByService = {
      'model:openrouter_backup': {
        has_key: true,
        source: 'ui',
        masked_key: 'sk-ob...9999',
        api_key: 'sk-openrouter-backup-real',
      },
    };

    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method || 'GET';
      const url = typeof input === 'string' ? input : input.toString();

      if (method === 'GET' && url.includes('/api-key')) {
        const service = extractService(url);
        const payload = keyStatusByService[service] || { has_key: false };
        return {
          ok: true,
          json: async () => payload,
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

    Object.defineProperty(globalThis, 'navigator', {
      value: { clipboard: { writeText: writeTextMock } },
      writable: true,
      configurable: true,
    });
  });

  it('renders configured rows', () => {
    render(<ModelConfig />);

    expect(screen.getByText('default')).toBeTruthy();
    expect(screen.getByText('anthropic')).toBeTruthy();
    expect(screen.getByText('openrouter')).toBeTruthy();
  });

  it('starts inline editing when a row is clicked', () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('anthropic'));

    expect(screen.getByDisplayValue('anthropic')).toBeTruthy();
    expect(screen.getByDisplayValue('claude-3-5-haiku-latest')).toBeTruthy();
  });

  it('saves inline name and model-id edits', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('anthropic'));

    const row = screen.getByDisplayValue('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    fireEvent.change(within(row).getByDisplayValue('anthropic'), {
      target: { value: 'anthropic-fast' },
    });
    fireEvent.change(within(row).getByDisplayValue('claude-3-5-haiku-latest'), {
      target: { value: 'claude-3-5-sonnet-latest' },
    });

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith(
        'anthropic-fast',
        expect.objectContaining({ provider: 'anthropic', id: 'claude-3-5-sonnet-latest' })
      );
      expect(mockStore.deleteModel).toHaveBeenCalledWith('anthropic');
    });
  });

  it('changes provider with inline dropdown', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('openrouter'));

    const row = screen.getByDisplayValue('openrouter').closest('tr');
    if (!row) throw new Error('row not found');

    fireEvent.click(within(row).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: /OpenAI/i }));

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith(
        'openrouter',
        expect.objectContaining({ provider: 'openai' })
      );
    });
  });

  it('shows API key source labels', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
      api_key: 'sk-anthropic-real',
    };
    keyStatusByService['openrouter'] = {
      has_key: true,
      source: 'env',
      masked_key: 'sk-en...5678',
      api_key: 'sk-openrouter-env-real',
    };

    render(<ModelConfig />);

    await waitFor(() => {
      expect(screen.getAllByText('Source: UI').length).toBeGreaterThan(0);
      expect(screen.getAllByText('Source: .env').length).toBeGreaterThan(0);
    });
  });

  it('reuses key from another same-provider model', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('openrouter'));

    const row = screen.getByDisplayValue('openrouter').closest('tr');
    if (!row) throw new Error('row not found');

    await waitFor(() => {
      expect(within(row).getByText('Reuse from same provider')).toBeTruthy();
    });

    const reuseTrigger = within(row).getByText('Reuse from same provider').closest('button');
    if (!reuseTrigger) throw new Error('reuse trigger not found');

    fireEvent.click(reuseTrigger);
    fireEvent.click(screen.getByRole('option', { name: /openrouter_backup/i }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/credentials/model:openrouter/copy-from/model:openrouter_backup',
        { method: 'POST' }
      );
    });
  });

  it('copies API key via copy button', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
      api_key: 'sk-anthropic-real',
    };

    render(<ModelConfig />);

    const row = screen.getByText('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    await waitFor(() => {
      expect(within(row).getByTitle('Copy API key')).toBeTruthy();
    });

    fireEvent.click(within(row).getByTitle('Copy API key'));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/credentials/model:anthropic/api-key?key_name=api_key&include_value=true'
      );
      expect(writeTextMock).toHaveBeenCalledWith('sk-anthropic-real');
    });
  });

  it('adds a model using the top add row', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));

    fireEvent.change(screen.getByPlaceholderText('model name'), {
      target: { value: 'new-model' },
    });
    fireEvent.change(screen.getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4o-mini' },
    });

    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('new-model', {
        provider: 'openrouter',
        id: 'gpt-4o-mini',
      });
    });
  });

  it('deletes non-default models and keeps default protected', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<ModelConfig />);

    const nonDefaultRow = screen.getByText('openrouter').closest('tr');
    const defaultRow = screen.getByText('default').closest('tr');
    if (!nonDefaultRow || !defaultRow) throw new Error('rows not found');

    fireEvent.click(within(nonDefaultRow).getByTitle('Delete'));
    expect(mockStore.deleteModel).toHaveBeenCalledWith('openrouter');

    expect(within(defaultRow).queryByTitle('Delete')).toBeNull();

    confirmSpy.mockRestore();
  });
});
