import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelConfig } from './ModelConfig';
import { useConfigStore } from '@/store/configStore';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
}));

describe('ModelConfig', () => {
  const mockStore = {
    config: {
      connections: {
        'anthropic/default': {
          provider: 'anthropic',
          service: 'anthropic',
          auth_kind: 'api_key',
        },
      },
      models: {
        default: { provider: 'ollama', id: 'devstral:24b' },
        anthropic: {
          provider: 'anthropic',
          id: 'claude-3-5-haiku-latest',
          connection: 'anthropic/team-a',
        },
        openrouter: { provider: 'openrouter', id: 'z-ai/glm-4.5-air:free' },
        openai_local: {
          provider: 'openai',
          id: 'gpt-4.1-mini',
          context_window: 16384,
          extra_kwargs: { base_url: 'http://localhost:9292/v1' },
        },
      },
      agents: {},
      defaults: { markdown: true },
      router: { model: 'default' },
    },
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue({ status: 'saved' }),
    isLoading: false,
  };

  beforeEach(() => {
    vi.clearAllMocks();

    const mockedUseConfigStore = useConfigStore as unknown as {
      mockReturnValue: (value: unknown) => void;
    };
    mockedUseConfigStore.mockReturnValue(mockStore);
  });

  it('renders configured rows and keeps the table scrollable', () => {
    render(<ModelConfig />);

    expect(screen.getByText('default')).toBeTruthy();
    expect(screen.getByText('anthropic')).toBeTruthy();
    expect(screen.getByText('openrouter')).toBeTruthy();
    expect(screen.getByText('openai_local')).toBeTruthy();

    const scrollContainer = screen.getByTestId('models-table-scroll-container');
    expect(scrollContainer).toHaveClass('overflow-x-auto');
    expect(within(scrollContainer).getByRole('table')).toBeTruthy();
  });

  it('shows explicit and default connection summaries', () => {
    render(<ModelConfig />);

    expect(screen.getByText('anthropic/team-a')).toBeInTheDocument();
    expect(screen.getByText('openrouter/default')).toBeInTheDocument();
    expect(screen.getByText('openai/default')).toBeInTheDocument();
  });

  it('starts inline editing when a row is clicked', () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByText('anthropic'));

    expect(screen.getByDisplayValue('anthropic')).toBeTruthy();
    expect(screen.getByDisplayValue('claude-3-5-haiku-latest')).toBeTruthy();
    expect(screen.getByDisplayValue('anthropic/team-a')).toBeTruthy();
  });

  it('saves inline name, model id, and connection edits', async () => {
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
    fireEvent.change(within(row).getByDisplayValue('anthropic/team-a'), {
      target: { value: 'anthropic/research' },
    });

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith(
        'anthropic-fast',
        expect.objectContaining({
          provider: 'anthropic',
          id: 'claude-3-5-sonnet-latest',
          connection: 'anthropic/research',
        })
      );
      expect(mockStore.deleteModel).toHaveBeenCalledWith('anthropic');
    });
  });

  it('shows OpenAI endpoint details and allows editing base URL', async () => {
    render(<ModelConfig />);

    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent?.includes('Endpoint: http://localhost:9292/v1') ?? false
      ).length
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(
        (_, element) => element?.textContent?.includes('Context window: 16,384') ?? false
      ).length
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByText('openai_local'));
    const row = screen.getByDisplayValue('openai_local').closest('tr');
    if (!row) throw new Error('row not found');

    expect(within(row).getByDisplayValue('http://localhost:9292/v1')).toBeTruthy();
    expect(within(row).getByDisplayValue('16384')).toBeTruthy();

    fireEvent.change(within(row).getByDisplayValue('http://localhost:9292/v1'), {
      target: { value: 'http://localhost:11434/v1' },
    });
    fireEvent.change(within(row).getByDisplayValue('16384'), {
      target: { value: '32768' },
    });
    fireEvent.change(within(row).getByPlaceholderText('openai/default'), {
      target: { value: 'openai/lab' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith(
        'openai_local',
        expect.objectContaining({
          provider: 'openai',
          id: 'gpt-4.1-mini',
          connection: 'openai/lab',
          context_window: 32768,
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

  it('adds a model using the top add row', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));

    expect(screen.getAllByText('openrouter/default').length).toBeGreaterThan(0);

    fireEvent.change(screen.getByPlaceholderText('model name'), {
      target: { value: 'new-model' },
    });
    fireEvent.change(screen.getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4o-mini' },
    });
    fireEvent.change(screen.getByPlaceholderText('optional context window'), {
      target: { value: '200000' },
    });
    fireEvent.change(screen.getByPlaceholderText('openrouter/default'), {
      target: { value: 'openrouter/research' },
    });

    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('new-model', {
        provider: 'openrouter',
        id: 'gpt-4o-mini',
        connection: 'openrouter/research',
        context_window: 200000,
      });
    });
  });

  it('accepts empty base URL for OpenAI and uses the default connection placeholder', async () => {
    render(<ModelConfig />);

    fireEvent.click(screen.getByRole('button', { name: /add model/i }));
    const addRow = screen.getByPlaceholderText('model name').closest('tr');
    if (!addRow) throw new Error('add row not found');

    fireEvent.click(within(addRow).getAllByRole('combobox')[0]);
    fireEvent.click(screen.getByRole('option', { name: /OpenAI/i }));

    fireEvent.change(within(addRow).getByPlaceholderText('model name'), {
      target: { value: 'openai_default' },
    });
    fireEvent.change(within(addRow).getByPlaceholderText('provider model id'), {
      target: { value: 'gpt-4.1-mini' },
    });

    expect(within(addRow).getByPlaceholderText('https://api.openai.com/v1')).toBeTruthy();
    expect(within(addRow).getByPlaceholderText('openai/default')).toBeTruthy();

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
    fireEvent.click(screen.getByRole('option', { name: /OpenAI/i }));

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

  it('shows a toast when Save All Changes is superseded by newer draft edits', async () => {
    mockStore.saveConfig.mockResolvedValueOnce({ status: 'stale' });
    const user = userEvent.setup();

    render(<ModelConfig />);

    await user.click(screen.getByRole('button', { name: 'Save All Changes' }));

    const { toast } = await import('@/components/ui/toaster');
    await waitFor(() => {
      expect(mockStore.saveConfig).toHaveBeenCalledTimes(1);
      expect(toast).toHaveBeenCalledWith({
        title: 'Save Failed',
        description: 'Save was superseded by newer draft edits.',
        variant: 'destructive',
      });
    });
  });
});
