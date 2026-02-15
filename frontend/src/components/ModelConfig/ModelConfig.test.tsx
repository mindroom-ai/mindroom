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
  const service = end === -1 ? rest : rest.slice(0, end);
  const queryIndex = service.indexOf('?');
  return queryIndex === -1 ? service : service.slice(0, queryIndex);
}

describe('ModelConfig', () => {
  const mockStore = {
    config: {
      models: {
        default: { provider: 'ollama', id: 'devstral:24b' },
        anthropic: { provider: 'anthropic', id: 'claude-3-5-haiku-latest' },
        openrouter: { provider: 'openrouter', id: 'z-ai/glm-4.5-air:free' },
        openrouter_backup: { provider: 'openrouter', id: 'openai/gpt-4o-mini' },
        openai_local: {
          provider: 'openai',
          id: 'gpt-4.1-mini',
          extra_kwargs: { base_url: 'http://localhost:9292/v1' },
        },
      },
      agents: {},
      defaults: { markdown: true },
      router: { model: 'default' },
    },
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue(undefined),
  };

  let keyStatusByService: Record<string, KeyStatusResponse>;
  let keyValueByService: Record<string, string>;
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
    keyValueByService = {
      'model:openrouter_backup': 'sk-openrouter-backup-real',
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

      if (method === 'GET' && url.includes('/api/credentials/')) {
        const service = extractService(url);
        const apiKey = keyValueByService[service];
        return {
          ok: true,
          json: async () => ({
            service,
            credentials: apiKey ? { api_key: apiKey } : {},
          }),
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
    expect(screen.getByText('openai_local')).toBeTruthy();
  });

  it('keeps the models table horizontally scrollable', () => {
    render(<ModelConfig />);

    const scrollContainer = screen.getByTestId('models-table-scroll-container');
    expect(scrollContainer).toHaveClass('overflow-x-auto');
    expect(within(scrollContainer).getByRole('table')).toBeTruthy();
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

  it('deletes old custom credential when renaming a model', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
      api_key: 'sk-anthropic-real',
    };

    render(<ModelConfig />);

    await waitFor(() => {
      expect(screen.getAllByText('Source: UI').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getByText('anthropic'));

    const row = screen.getByDisplayValue('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    fireEvent.change(within(row).getByDisplayValue('anthropic'), {
      target: { value: 'anthropic-renamed' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/credentials/model:anthropic-renamed/copy-from/model:anthropic',
        { method: 'POST', headers: expect.any(Object) }
      );
      expect(fetchMock).toHaveBeenCalledWith('/api/credentials/model:anthropic', {
        method: 'DELETE',
        headers: expect.any(Object),
      });
    });
  });

  it('keeps focus in model id input while typing', () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('anthropic'));

    const row = screen.getByDisplayValue('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    const modelIdInput = within(row).getByDisplayValue('claude-3-5-haiku-latest');
    modelIdInput.focus();
    expect(modelIdInput).toHaveFocus();

    fireEvent.change(modelIdInput, { target: { value: 'claude-3-5-haiku-latesta' } });

    const updatedInput = within(row).getByDisplayValue('claude-3-5-haiku-latesta');
    expect(updatedInput).toBe(modelIdInput);
    expect(updatedInput).toHaveFocus();
  });

  it('shows OpenAI endpoint details and allows editing base URL', async () => {
    render(<ModelConfig />);

    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent?.includes('Endpoint: http://localhost:9292/v1') ?? false
      ).length
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByText('openai_local'));
    const row = screen.getByDisplayValue('openai_local').closest('tr');
    if (!row) throw new Error('row not found');

    expect(within(row).getByDisplayValue('http://localhost:9292/v1')).toBeTruthy();

    fireEvent.change(within(row).getByDisplayValue('http://localhost:9292/v1'), {
      target: { value: 'http://localhost:11434/v1' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith(
        'openai_local',
        expect.objectContaining({
          provider: 'openai',
          id: 'gpt-4.1-mini',
          extra_kwargs: { base_url: 'http://localhost:11434/v1' },
        })
      );
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
      expect(
        screen.getAllByText((_, element) => element?.textContent?.includes('Source: .env') ?? false)
          .length
      ).toBeGreaterThan(0);
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
        { method: 'POST', headers: expect.any(Object) }
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
        '/api/credentials/model:anthropic/api-key?key_name=api_key&include_value=true',
        expect.objectContaining({
          headers: expect.any(Object),
        })
      );
      expect(writeTextMock).toHaveBeenCalledWith('sk-anthropic-real');
    });
  });

  it('copies API key via legacy credentials endpoint fallback', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
    };
    keyValueByService['model:anthropic'] = 'sk-anthropic-legacy-real';

    render(<ModelConfig />);

    const row = screen.getByText('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    await waitFor(() => {
      expect(within(row).getByTitle('Copy API key')).toBeTruthy();
    });

    fireEvent.click(within(row).getByTitle('Copy API key'));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/credentials/model:anthropic/api-key?key_name=api_key&include_value=true',
        expect.objectContaining({
          headers: expect.any(Object),
        })
      );
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/credentials/model:anthropic',
        expect.objectContaining({
          headers: expect.any(Object),
        })
      );
      expect(writeTextMock).toHaveBeenCalledWith('sk-anthropic-legacy-real');
    });
  });

  it('adds a model using the top add row', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));

    expect(
      screen.getByText(
        'No custom key provided. This model will use the provider key (for example from .env) when available.'
      )
    ).toBeTruthy();

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

  it('accepts empty base URL for OpenAI and uses default endpoint', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));
    const addRow = screen.getByPlaceholderText('model name').closest('tr');
    if (!addRow) throw new Error('add row not found');

    fireEvent.click(within(addRow).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: /OpenAI OpenAI/i }));

    fireEvent.change(within(addRow).getByPlaceholderText('model name'), {
      target: { value: 'openai_default' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4.1-mini' },
    });

    expect(within(addRow).getByPlaceholderText('https://api.openai.com/v1')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('openai_default', {
        provider: 'openai',
        id: 'gpt-4.1-mini',
      });
    });
  });

  it('validates custom OpenAI base URL on add', async () => {
    const { toast } = await import('@/components/ui/toaster');

    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));
    const addRow = screen.getByPlaceholderText('model name').closest('tr');
    if (!addRow) throw new Error('add row not found');

    fireEvent.click(within(addRow).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: /OpenAI OpenAI/i }));

    fireEvent.change(within(addRow).getByPlaceholderText('model name'), {
      target: { value: 'openai_compat' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4.1-mini' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('https://api.openai.com/v1'), {
      target: { value: 'not-a-url' },
    });

    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Error',
          description: 'Base URL must be a valid http(s) URL',
          variant: 'destructive',
        })
      );
    });
  });

  it('saves custom OpenAI base URL on add', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));
    const addRow = screen.getByPlaceholderText('model name').closest('tr');
    if (!addRow) throw new Error('add row not found');

    fireEvent.click(within(addRow).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: /OpenAI OpenAI/i }));

    fireEvent.change(within(addRow).getByPlaceholderText('model name'), {
      target: { value: 'openai_compat' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4.1-mini' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('https://api.openai.com/v1'), {
      target: { value: 'http://localhost:9292/v1' },
    });

    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('openai_compat', {
        provider: 'openai',
        id: 'gpt-4.1-mini',
        extra_kwargs: { base_url: 'http://localhost:9292/v1' },
      });
    });
  });

  it('shows immediate feedback when clearing a custom key', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
      api_key: 'sk-anthropic-real',
    };

    render(<ModelConfig />);

    await waitFor(() => {
      expect(screen.getAllByText('Source: UI').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getByText('anthropic'));
    const row = screen.getByDisplayValue('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    fireEvent.click(within(row).getByRole('button', { name: 'Clear custom key' }));

    expect(within(row).getByText('Custom key will be removed on save.')).toBeTruthy();
    expect(within(row).getByRole('button', { name: 'Undo clear key' })).toBeTruthy();

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/credentials/model:anthropic', {
        method: 'DELETE',
        headers: expect.any(Object),
      });
    });
  });

  it('deletes custom key only once when clearing key and renaming model', async () => {
    keyStatusByService['model:anthropic'] = {
      has_key: true,
      source: 'ui',
      masked_key: 'sk-an...1234',
      api_key: 'sk-anthropic-real',
    };

    render(<ModelConfig />);

    await waitFor(() => {
      expect(screen.getAllByText('Source: UI').length).toBeGreaterThan(0);
    });

    fireEvent.click(screen.getByText('anthropic'));
    const row = screen.getByDisplayValue('anthropic').closest('tr');
    if (!row) throw new Error('row not found');

    fireEvent.change(within(row).getByDisplayValue('anthropic'), {
      target: { value: 'anthropic-cleared' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Clear custom key' }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      const deleteCalls = fetchMock.mock.calls.filter(
        ([url, init]) =>
          url === '/api/credentials/model:anthropic' &&
          typeof init === 'object' &&
          init?.method === 'DELETE'
      );
      expect(deleteCalls).toHaveLength(1);
      expect(fetchMock).not.toHaveBeenCalledWith(
        '/api/credentials/model:anthropic-cleared/copy-from/model:anthropic',
        { method: 'POST' }
      );
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
