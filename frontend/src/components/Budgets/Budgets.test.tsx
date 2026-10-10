import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Budgets } from './Budgets';
import { useConfigStore } from '@/store/configStore';
import type { Config } from '@/types/config';

vi.mock('@/store/configStore');
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const mockUpdateConfigValue = vi.fn();
const mockSaveConfig = vi.fn();

const status = {
  enabled: true,
  period_start: '2026-10-01',
  period_end: '2026-11-01',
  generated_at: '2026-10-09T12:00:00+00:00',
  default_limit_usd: 20,
  fallback_model: 'luna',
  users: [
    {
      user_id: '@alice:example.org',
      spend_usd: 25.5,
      limit_usd: 20,
      over_budget: true,
    },
    {
      user_id: '@bob:example.org',
      spend_usd: 3.25,
      limit_usd: 100,
      over_budget: false,
    },
  ],
  unpriced_models: [{ provider: 'Ollama', model: 'qwen3.8:27b', total_tokens: 12345 }],
  coverage: { scanned_sources: 3, unavailable_sources: 0 },
};

function budgetConfig(overrides: Partial<Config> = {}): Config {
  return {
    models: {
      astra: {
        provider: 'openai',
        id: 'gpt-6-astra',
        pricing: { input: 5, output: 30 },
      },
      luna: {
        provider: 'openai',
        id: 'gpt-6-luna',
        pricing: { input: 0.2, output: 1.25 },
      },
      local: { provider: 'ollama', id: 'qwen3.8:27b' },
    },
    budgets: {
      monthly_limit_usd: 20,
      fallback_model: 'luna',
      users: { '@bob:example.org': 100 },
    },
    ...overrides,
  } as Config;
}

function setStore(config: Config | null) {
  vi.mocked(useConfigStore).mockReturnValue({
    config,
    isDirty: true,
    isLoading: false,
    saveConfig: mockSaveConfig,
    updateConfigValue: mockUpdateConfigValue,
  } as unknown as ReturnType<typeof useConfigStore>);
}

function respond(payload: unknown, statusCode = 200) {
  return new Response(JSON.stringify(payload), {
    status: statusCode,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderBudgets() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const page = () => (
    <QueryClientProvider client={client}>
      <Budgets />
    </QueryClientProvider>
  );
  const result = render(page());
  // The mocked store is not reactive; rerender to show a draft edit.
  return { ...result, rerenderPage: () => result.rerender(page()) };
}

beforeEach(() => {
  vi.mocked(fetch).mockReset();
  mockUpdateConfigValue.mockReset();
  mockSaveConfig.mockReset();
  setStore(budgetConfig());
});
afterEach(() => cleanup());

describe('Budgets', () => {
  it("shows each user's month-to-date spend against their cap", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));

    renderBudgets();

    const alice = await screen.findByRole('row', {
      name: /@alice:example.org/,
    });
    expect(within(alice).getByText('$25.50')).toBeInTheDocument();
    expect(within(alice).getByText('Over budget, using luna')).toBeInTheDocument();
    const bob = screen.getByRole('row', { name: /@bob:example.org/ });
    expect(within(bob).getByText('$3.25')).toBeInTheDocument();
    expect(within(bob).getByText('Within budget')).toBeInTheDocument();
    expect(
      within(bob).getByRole('spinbutton', {
        name: 'Monthly cap for @bob:example.org',
      })
    ).toHaveValue(100);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/budgets'),
      expect.objectContaining({ cache: 'no-store' })
    );
  });

  it('keeps cents of a cent visible for small spend and caps', async () => {
    setStore(
      budgetConfig({
        budgets: { monthly_limit_usd: 0.005, fallback_model: 'luna' },
      })
    );
    vi.mocked(fetch).mockResolvedValue(
      respond({
        ...status,
        default_limit_usd: 0.005,
        users: [
          {
            user_id: '@alice:example.org',
            spend_usd: 0.011,
            limit_usd: 0.005,
            over_budget: true,
          },
        ],
      })
    );

    renderBudgets();

    const alice = await screen.findByRole('row', {
      name: /@alice:example.org/,
    });
    expect(within(alice).getByText('$0.011')).toBeInTheDocument();
    expect(
      within(alice).getByRole('spinbutton', {
        name: 'Monthly cap for @alice:example.org',
      })
    ).toHaveAttribute('placeholder', 'Default ($0.005)');
  });

  it('marks cache prices that fall back to the input price', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    expect(
      await screen.findByRole('spinbutton', { name: 'astra cache read price' })
    ).toHaveAttribute('placeholder', '= input');
  });

  it('shows a cap keyed by a bridge alias on its canonical user', async () => {
    setStore(
      budgetConfig({
        authorization: {
          aliases: { '@alice:example.org': ['@tg-alice:example.org'] },
        },
        budgets: {
          monthly_limit_usd: 20,
          fallback_model: 'luna',
          users: { '@tg-alice:example.org': 100 },
        },
      } as Partial<Config>)
    );
    vi.mocked(fetch).mockResolvedValue(
      respond({
        ...status,
        users: [
          {
            user_id: '@alice:example.org',
            spend_usd: 25.5,
            limit_usd: 100,
            over_budget: false,
          },
        ],
      })
    );
    renderBudgets();

    const alice = await screen.findByRole('row', {
      name: /@alice:example.org/,
    });
    const cap = within(alice).getByRole('spinbutton', {
      name: 'Monthly cap for @alice:example.org',
    });
    expect(cap).toHaveValue(100);
    expect(within(alice).getByText('Within budget')).toBeInTheDocument();
    expect(screen.queryByRole('row', { name: /@tg-alice:example.org/ })).not.toBeInTheDocument();

    fireEvent.change(cap, { target: { value: '120' } });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['budgets', 'users', '@tg-alice:example.org'],
      120
    );
  });

  it("writes a user's cap override and clears it back to the default", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();
    const aliceCap = await screen.findByRole('spinbutton', {
      name: 'Monthly cap for @alice:example.org',
    });

    fireEvent.change(aliceCap, { target: { value: '50' } });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['budgets', 'users', '@alice:example.org'],
      50
    );

    fireEvent.change(
      screen.getByRole('spinbutton', {
        name: 'Monthly cap for @bob:example.org',
      }),
      { target: { value: '' } }
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['budgets', 'users', '@bob:example.org'],
      undefined
    );
  });

  it('adds a cap for a user who has not spent anything yet', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();
    await screen.findByRole('row', { name: /@alice:example.org/ });

    fireEvent.change(screen.getByPlaceholderText('@user:example.com'), {
      target: { value: '@carol:example.org' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'New user cap' }), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add user' }));

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['budgets', 'users', '@carol:example.org'],
      5
    );
  });

  it("keeps a user's row while their cap is cleared and retyped", async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ ...status, users: [status.users[0]] }));
    const { rerenderPage } = renderBudgets();
    await screen.findByRole('row', { name: /@alice:example.org/ });

    fireEvent.change(
      screen.getByRole('spinbutton', {
        name: 'Monthly cap for @bob:example.org',
      }),
      { target: { value: '' } }
    );
    setStore(
      budgetConfig({
        budgets: { monthly_limit_usd: 20, fallback_model: 'luna', users: {} },
      })
    );
    rerenderPage();

    expect(
      screen.getByRole('spinbutton', {
        name: 'Monthly cap for @bob:example.org',
      })
    ).toBeInTheDocument();
  });

  it('edits the default cap and the fallback model', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(await screen.findByRole('spinbutton', { name: 'Default monthly cap' }), {
      target: { value: '' },
    });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['budgets', 'monthly_limit_usd'],
      undefined
    );

    fireEvent.change(screen.getByRole('combobox', { name: 'Fallback model' }), {
      target: { value: 'local' },
    });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['budgets', 'fallback_model'], 'local');
  });

  it('turns budgets on with the cheapest priced model as fallback', async () => {
    setStore(budgetConfig({ budgets: undefined }));
    vi.mocked(fetch).mockResolvedValue(respond({ enabled: false }));
    renderBudgets();

    fireEvent.click(await screen.findByRole('button', { name: 'Turn on budgets' }));

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['budgets'], {
      fallback_model: 'luna',
    });
    expect(
      screen.queryByRole('spinbutton', { name: 'Default monthly cap' })
    ).not.toBeInTheDocument();
  });

  it('turns budgets off', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.click(await screen.findByRole('button', { name: 'Turn off budgets' }));

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['budgets'], undefined);
  });

  it('edits model prices and removes them when every price is cleared', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(
      await screen.findByRole('spinbutton', {
        name: 'local input price',
      }),
      { target: { value: '0.5' } }
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['models', 'local', 'pricing'], {
      input: 0.5,
    });

    fireEvent.change(screen.getByRole('spinbutton', { name: 'astra cache read price' }), {
      target: { value: '0.5' },
    });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['models', 'astra', 'pricing'], {
      input: 5,
      output: 30,
      cache_read: 0.5,
    });

    fireEvent.change(screen.getByRole('spinbutton', { name: 'luna input price' }), {
      target: { value: '' },
    });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(['models', 'luna', 'pricing'], {
      output: 1.25,
    });
  });

  it("removes a model's pricing once its last price is cleared", async () => {
    setStore(
      budgetConfig({
        models: {
          luna: {
            provider: 'openai',
            id: 'gpt-6-luna',
            pricing: { input: 0.2 },
          },
        } as Config['models'],
      })
    );
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(await screen.findByRole('spinbutton', { name: 'luna input price' }), {
      target: { value: '' },
    });

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ['models', 'luna', 'pricing'],
      undefined
    );
  });

  it('warns about models with usage but no prices', async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    expect(await screen.findByText(/Ollama qwen3\.8:27b/)).toBeInTheDocument();
    expect(screen.getByText(/12,345 tokens/)).toBeInTheDocument();
  });

  it('keeps settings editable when the runtime cannot report spend', async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ detail: 'Budget monitor unavailable' }, 503));
    renderBudgets();

    expect(await screen.findByText(/Spend is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: 'Default monthly cap' })).toHaveValue(20);
    expect(
      screen.getByRole('spinbutton', {
        name: 'Monthly cap for @bob:example.org',
      })
    ).toHaveValue(100);
  });

  it('saves the draft config', async () => {
    mockSaveConfig.mockResolvedValue({ status: 'saved' });
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(mockSaveConfig).toHaveBeenCalledTimes(1);
  });
});
