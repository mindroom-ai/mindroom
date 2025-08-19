import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { ModelConfig } from './ModelConfig';
import { useConfigStore } from '@/store/configStore';

// Mock the store
vi.mock('@/store/configStore');

// Mock the toaster
vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
}));

describe('ModelConfig - extra_kwargs functionality', () => {
  let mockStore: any;

  beforeEach(() => {
    mockStore = {
      config: {
        models: {
          'test-model': {
            provider: 'openrouter',
            id: 'openai/gpt-4',
            extra_kwargs: {
              request_params: {
                provider: {
                  order: ['Cerebras'],
                  allow_fallbacks: false,
                },
              },
            },
          },
        },
        agents: {},
        defaults: {
          num_history_runs: 5,
          markdown: true,
          add_history_to_messages: true,
        },
        router: {
          model: 'test-model',
        },
      },
      updateModel: vi.fn(),
      deleteModel: vi.fn(),
      setAPIKey: vi.fn(),
      testModel: vi.fn().mockResolvedValue(true),
      saveConfig: vi.fn(),
      apiKeys: {},
    };

    (useConfigStore as any).mockReturnValue(mockStore);
  });

  it('displays extra_kwargs in read-only view when configured', () => {
    render(<ModelConfig />);

    // Check that the advanced settings are displayed
    const advancedText = screen.getByText(/Advanced:/);
    expect(advancedText).toBeInTheDocument();

    // The JSON should be displayed (truncated)
    const jsonDisplay = screen.getByText((content, element) => {
      return element?.tagName === 'CODE' && content.includes('request_params');
    });
    expect(jsonDisplay).toBeInTheDocument();
  });

  it('shows extra_kwargs JSON editor when editing a model', () => {
    render(<ModelConfig />);

    // Click the edit button
    const editButton = screen.getByRole('button', { name: 'Edit' });
    fireEvent.click(editButton);

    // Find the advanced settings textarea
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    expect(advancedTextarea).toBeInTheDocument();

    // Check that it contains the formatted JSON
    const value = (advancedTextarea as HTMLTextAreaElement).value;
    const parsed = JSON.parse(value);
    expect(parsed.request_params.provider.order).toEqual(['Cerebras']);
  });

  it('allows adding extra_kwargs when creating a new model', () => {
    render(<ModelConfig />);

    // Click Add New Model
    const addButton = screen.getByRole('button', { name: /Add New Model/i });
    fireEvent.click(addButton);

    // Find the advanced settings textarea
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    expect(advancedTextarea).toBeInTheDocument();

    // Check the placeholder for OpenRouter
    const placeholder = (advancedTextarea as HTMLTextAreaElement).placeholder;
    expect(placeholder).toContain('request_params');
    expect(placeholder).toContain('Cerebras');
  });

  it('validates JSON format in extra_kwargs field', async () => {
    const { toast } = await import('@/components/ui/toaster');

    render(<ModelConfig />);

    // Click Add New Model
    const addButton = screen.getByRole('button', { name: /Add New Model/i });
    fireEvent.click(addButton);

    // Fill in required fields
    const configNameInput = screen.getByLabelText(/Configuration Name/);
    fireEvent.change(configNameInput, { target: { value: 'test-config' } });

    const modelIdInput = screen.getByLabelText(/Model ID/);
    fireEvent.change(modelIdInput, { target: { value: 'test-model-id' } });

    // Add invalid JSON to extra_kwargs
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    fireEvent.change(advancedTextarea, { target: { value: '{ invalid json' } });

    // Try to save
    const saveButton = screen.getByRole('button', { name: /Add Model/i });
    fireEvent.click(saveButton);

    // Check that an error toast was shown
    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Invalid JSON',
          description: 'The Advanced Settings must be valid JSON',
          variant: 'destructive',
        })
      );
    });
  });

  it('saves model with valid extra_kwargs JSON', async () => {
    render(<ModelConfig />);

    // Click Add New Model
    const addButton = screen.getByRole('button', { name: /Add New Model/i });
    fireEvent.click(addButton);

    // Fill in required fields
    const configNameInput = screen.getByLabelText(/Configuration Name/);
    fireEvent.change(configNameInput, { target: { value: 'new-model' } });

    const modelIdInput = screen.getByLabelText(/Model ID/);
    fireEvent.change(modelIdInput, { target: { value: 'gpt-4' } });

    // Add valid JSON to extra_kwargs
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    const extraKwargs = {
      request_params: {
        provider: {
          order: ['Cerebras', 'Together AI'],
          allow_fallbacks: true,
        },
      },
      temperature: 0.7,
    };
    fireEvent.change(advancedTextarea, {
      target: { value: JSON.stringify(extraKwargs, null, 2) },
    });

    // Save
    const saveButton = screen.getByRole('button', { name: /Add Model/i });
    fireEvent.click(saveButton);

    // Check that updateModel was called with the parsed extra_kwargs
    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('new-model', {
        provider: 'openrouter',
        id: 'gpt-4',
        extra_kwargs: extraKwargs,
      });
    });
  });

  it('updates existing model with extra_kwargs', async () => {
    render(<ModelConfig />);

    // Click the edit button
    const editButton = screen.getByRole('button', { name: 'Edit' });
    fireEvent.click(editButton);

    // Modify the extra_kwargs
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    const newExtraKwargs = {
      request_params: {
        provider: {
          only: ['OpenAI'],
        },
      },
      max_tokens: 2048,
    };
    fireEvent.change(advancedTextarea, {
      target: { value: JSON.stringify(newExtraKwargs, null, 2) },
    });

    // Save
    const saveButton = screen.getByRole('button', { name: 'Save' });
    fireEvent.click(saveButton);

    // Check that updateModel was called with the new extra_kwargs
    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('test-model', {
        provider: 'openrouter',
        id: 'openai/gpt-4',
        extra_kwargs: newExtraKwargs,
      });
    });
  });

  it('shows different placeholder for non-OpenRouter providers', () => {
    render(<ModelConfig />);

    // Click Add New Model
    const addButton = screen.getByRole('button', { name: /Add New Model/i });
    fireEvent.click(addButton);

    // Change provider to OpenAI
    const providerSelect = screen.getByRole('combobox');
    fireEvent.click(providerSelect);
    const openaiOption = screen.getByRole('option', { name: 'OpenAI' });
    fireEvent.click(openaiOption);

    // Check the placeholder changed
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    const placeholder = (advancedTextarea as HTMLTextAreaElement).placeholder;
    expect(placeholder).toContain('temperature');
    expect(placeholder).toContain('max_tokens');
    expect(placeholder).not.toContain('request_params');
  });

  it('handles empty extra_kwargs gracefully', async () => {
    render(<ModelConfig />);

    // Click Add New Model
    const addButton = screen.getByRole('button', { name: /Add New Model/i });
    fireEvent.click(addButton);

    // Fill in required fields
    const configNameInput = screen.getByLabelText(/Configuration Name/);
    fireEvent.change(configNameInput, { target: { value: 'simple-model' } });

    const modelIdInput = screen.getByLabelText(/Model ID/);
    fireEvent.change(modelIdInput, { target: { value: 'gpt-3.5' } });

    // Leave extra_kwargs empty
    const advancedTextarea = screen.getByLabelText(/Advanced Settings \(JSON\)/);
    expect((advancedTextarea as HTMLTextAreaElement).value).toBe('');

    // Save
    const saveButton = screen.getByRole('button', { name: /Add Model/i });
    fireEvent.click(saveButton);

    // Check that updateModel was called without extra_kwargs
    await waitFor(() => {
      expect(mockStore.updateModel).toHaveBeenCalledWith('simple-model', {
        provider: 'openrouter',
        id: 'gpt-3.5',
      });
    });
  });
});
