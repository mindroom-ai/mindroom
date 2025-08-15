import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ModelConfig } from './ModelConfig';
import { useConfigStore } from '@/store/configStore';

// Mock the store
vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

// Mock the toast
vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
}));

// Mock fetch globally for ApiKeyConfig component
global.fetch = vi.fn();

describe('ModelConfig', () => {
  const mockConfig = {
    models: {
      default: { provider: 'ollama', id: 'devstral:24b' },
      anthropic: { provider: 'anthropic', id: 'claude-3-5-haiku-latest' },
      openrouter: { provider: 'openrouter', id: 'z-ai/glm-4.5-air:free' },
    },
    agents: {},
    defaults: { num_history_runs: 5 },
  };

  const mockStore = {
    config: mockConfig,
    updateModel: vi.fn(),
    deleteModel: vi.fn(),
    setAPIKey: vi.fn(),
    testModel: vi.fn().mockResolvedValue(true),
    saveConfig: vi.fn().mockResolvedValue(undefined),
    apiKeys: {},
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue(mockStore);
    // Mock fetch responses for ApiKeyConfig component
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ has_key: false }),
    });
  });

  it('renders existing models', () => {
    render(<ModelConfig />);

    // Look for model names in card headers - they include buttons so we need to check for substring
    const modelCards = screen.getAllByRole('heading', { level: 3 });
    const modelText = modelCards.map(card => card.textContent).join(' ');

    expect(modelText).toContain('default');
    expect(modelText).toContain('anthropic');
    expect(modelText).toContain('openrouter');
  });

  it('shows add new model button', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    expect(addButton).toBeTruthy();
  });

  it('shows new model form when add button is clicked', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    // Check for the heading
    const heading = screen.getByRole('heading', { name: /add new model/i });
    expect(heading).toBeTruthy();

    // Check for form fields
    expect(screen.getByPlaceholderText(/openrouter-gpt4/i)).toBeTruthy();
    expect(screen.getByRole('combobox')).toBeTruthy();
    expect(screen.getByPlaceholderText(/gpt-4, claude-3-opus/i)).toBeTruthy();
  });

  it('creates a new model with valid data', async () => {
    render(<ModelConfig />);

    // Click add new model
    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    // Fill in the form
    const configNameInput = screen.getByPlaceholderText(/openrouter-gpt4/i);
    const modelIdInput = screen.getByPlaceholderText(/gpt-4, claude-3-opus/i);
    const providerSelect = screen.getByRole('combobox');

    fireEvent.change(configNameInput, { target: { value: 'openrouter-gpt4' } });
    fireEvent.change(modelIdInput, { target: { value: 'openai/gpt-4' } });

    // Provider should already be openrouter by default in the form
    // But let's click it to be sure
    fireEvent.click(providerSelect);
    const openrouterOption = screen.getByRole('option', { name: 'OpenRouter' });
    fireEvent.click(openrouterOption);

    // Submit
    const submitButton = screen.getByRole('button', { name: /add model/i });
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('openrouter-gpt4', {
        provider: 'openrouter',
        id: 'openai/gpt-4',
      });
    });
  });

  it('prevents creating model with duplicate config name', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    // Try to use existing config name
    const configNameInput = screen.getByPlaceholderText(/openrouter-gpt4/i);
    const modelIdInput = screen.getByPlaceholderText(/gpt-4, claude-3-opus/i);

    fireEvent.change(configNameInput, { target: { value: 'default' } });
    fireEvent.change(modelIdInput, { target: { value: 'some-model' } });

    const submitButton = screen.getByRole('button', { name: /add model/i });
    fireEvent.click(submitButton);

    expect(mockStore.updateModel).not.toHaveBeenCalled();
  });

  it('deletes a model when delete button is clicked', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<ModelConfig />);

    // Find delete button for anthropic model (not the default)
    const deleteButtons = screen
      .getAllByRole('button')
      .filter(btn => btn.querySelector('.lucide-trash2'));

    // Should have delete buttons for non-default models
    expect(deleteButtons.length).toBeGreaterThan(0);

    fireEvent.click(deleteButtons[0]);

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockStore.deleteModel).toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it('prevents deleting the default model', () => {
    render(<ModelConfig />);

    // Find all cards
    const allCards = screen.getAllByRole('heading', { level: 3 });
    const defaultCard = allCards.find(card => card.textContent?.includes('default'));

    // Check that the default card's parent doesn't have a delete button with trash icon
    const cardContainer = defaultCard?.parentElement?.parentElement;
    const trashButtons = cardContainer?.querySelectorAll('.lucide-trash2') || [];

    expect(trashButtons.length).toBe(0);
  });

  it('allows editing existing models', () => {
    render(<ModelConfig />);

    // Find edit button for a model
    const editButtons = screen.getAllByRole('button', { name: /edit/i });
    fireEvent.click(editButtons[0]);

    // Should show save and cancel buttons - there will be multiple save buttons
    const saveButtons = screen.getAllByRole('button', { name: /save/i });
    expect(saveButtons.length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /cancel/i })).toBeTruthy();
  });

  it('cancels model creation when cancel is clicked', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    const heading = screen.getByRole('heading', { name: /add new model/i });
    expect(heading).toBeTruthy();

    const cancelButton = screen.getByRole('button', { name: /cancel/i });
    fireEvent.click(cancelButton);

    // Form should disappear
    expect(screen.queryByRole('heading', { name: /add new model/i })).toBeFalsy();
    // Add button should reappear
    expect(screen.getByRole('button', { name: /add new model/i })).toBeTruthy();
  });

  it('shows host field for ollama provider', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    // Select ollama provider
    const providerSelect = screen.getByRole('combobox');
    fireEvent.click(providerSelect);
    const ollamaOption = screen.getByRole('option', { name: 'Ollama' });
    fireEvent.click(ollamaOption);

    // Host field should appear
    expect(screen.getByPlaceholderText('http://localhost:11434')).toBeTruthy();
  });

  it('hides host field for non-ollama providers', () => {
    render(<ModelConfig />);

    const addButton = screen.getByRole('button', { name: /add new model/i });
    fireEvent.click(addButton);

    // Select openrouter provider (should be default)
    const providerSelect = screen.getByRole('combobox');
    fireEvent.click(providerSelect);
    const openrouterOption = screen.getByRole('option', { name: 'OpenRouter' });
    fireEvent.click(openrouterOption);

    // Host field should not appear
    expect(screen.queryByPlaceholderText('http://localhost:11434')).toBeFalsy();
  });
});
