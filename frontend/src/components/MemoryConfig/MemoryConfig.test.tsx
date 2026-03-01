import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryConfig } from './MemoryConfig';
import { useConfigStore } from '@/store/configStore';
import { Config } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('MemoryConfig', () => {
  const mockConfig: Partial<Config> = {
    memory: {
      embedder: {
        provider: 'openai',
        config: {
          model: 'text-embedding-3-small',
        },
      },
    },
  };

  const mockUpdateMemoryConfig = vi.fn();
  const mockSaveConfig = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      config: mockConfig,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });
  });

  it('renders memory configuration', () => {
    render(<MemoryConfig />);

    expect(screen.getByText('Memory Configuration')).toBeInTheDocument();
    expect(screen.getByText(/Configure the embedder for agent memory/)).toBeInTheDocument();
  });

  it('displays current configuration', () => {
    render(<MemoryConfig />);

    expect(screen.getByText('Current Configuration')).toBeInTheDocument();
    expect(screen.getByText('openai', { selector: '.font-mono' })).toBeInTheDocument();
    expect(
      screen.getByText('text-embedding-3-small', { selector: '.font-mono' })
    ).toBeInTheDocument();
  });

  it('shows correct provider in select', () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    expect(providerSelect).toBeInTheDocument();
    expect(providerSelect).toHaveTextContent('OpenAI');
  });

  it('shows model as free-text input', () => {
    render(<MemoryConfig />);

    const modelInput = document.getElementById('model') as HTMLInputElement;
    expect(modelInput).toBeInTheDocument();
    expect(modelInput).toHaveValue('text-embedding-3-small');
    expect(modelInput.tagName).toBe('INPUT');
  });

  it('shows host input for openai provider', () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById('host') as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
  });

  it('updates team reads member memory toggle', async () => {
    render(<MemoryConfig />);

    const toggle = document.getElementById('team-reads-member-memory');
    expect(toggle).toBeInTheDocument();
    fireEvent.click(toggle!);

    const enabledOption = await screen.findByText('Enabled');
    fireEvent.click(enabledOption);

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          team_reads_member_memory: true,
        })
      );
    });
  });

  it('shows host input for ollama provider', () => {
    const ollamaConfig: Partial<Config> = {
      memory: {
        embedder: {
          provider: 'ollama',
          config: {
            model: 'nomic-embed-text',
            host: 'http://localhost:11434',
          },
        },
      },
    };

    (useConfigStore as any).mockReturnValue({
      config: ollamaConfig,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<MemoryConfig />);

    const hostInput = document.getElementById('host') as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue('http://localhost:11434');
  });

  it('changes provider and resets model to default', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    expect(providerSelect).toBeInTheDocument();
    fireEvent.click(providerSelect!);

    const ollamaOption = await screen.findByText('Ollama');
    fireEvent.click(ollamaOption);

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: 'ollama',
            config: expect.objectContaining({
              model: 'nomic-embed-text',
              host: 'http://localhost:11434',
            }),
          }),
        })
      );
    });
  });

  it('shows API key notice for OpenAI without custom base URL', () => {
    render(<MemoryConfig />);

    expect(screen.getByText(/OPENAI_API_KEY/)).toBeInTheDocument();
  });

  it('hides API key notice when base URL is set', () => {
    const configWithHost: Partial<Config> = {
      memory: {
        embedder: {
          provider: 'openai',
          config: {
            model: 'text-embedding-3-small',
            host: 'http://localhost:9292/v1',
          },
        },
      },
    };

    (useConfigStore as any).mockReturnValue({
      config: configWithHost,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<MemoryConfig />);

    expect(screen.queryByText(/OPENAI_API_KEY/)).not.toBeInTheDocument();
  });

  it('allows typing custom model name', async () => {
    render(<MemoryConfig />);

    const modelInput = document.getElementById('model') as HTMLInputElement;
    fireEvent.change(modelInput, { target: { value: 'my-custom-embedding-model' } });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: 'openai',
            config: expect.objectContaining({
              model: 'my-custom-embedding-model',
              host: '',
            }),
          }),
        })
      );
    });
  });

  it('updates host when input changes', async () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById('host') as HTMLInputElement;
    fireEvent.change(hostInput, { target: { value: 'http://localhost:9292/v1' } });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          embedder: expect.objectContaining({
            provider: 'openai',
            config: expect.objectContaining({
              model: 'text-embedding-3-small',
              host: 'http://localhost:9292/v1',
            }),
          }),
        })
      );
    });
  });

  it('calls saveConfig when save button is clicked', async () => {
    // Re-mock with isDirty: true so the button is enabled
    (useConfigStore as any).mockReturnValue({
      config: mockConfig,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: true,
    });

    render(<MemoryConfig />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });

  it('disables save button when not dirty', () => {
    render(<MemoryConfig />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).toBeDisabled();
  });

  it('enables save button when dirty', () => {
    (useConfigStore as any).mockReturnValue({
      config: mockConfig,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: true,
    });

    render(<MemoryConfig />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
  });

  it('shows provider description for openai', () => {
    render(<MemoryConfig />);

    expect(
      screen.getByText('OpenAI or any OpenAI-compatible API (set Base URL below)')
    ).toBeInTheDocument();
  });

  it('shows provider description for ollama', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);
    const ollamaOption = await screen.findByText('Ollama');
    fireEvent.click(ollamaOption);

    await waitFor(() => {
      expect(screen.getByText('Local embeddings using Ollama')).toBeInTheDocument();
    });
  });

  it('shows base URL in config display when set', () => {
    const configWithHost: Partial<Config> = {
      memory: {
        embedder: {
          provider: 'openai',
          config: {
            model: 'text-embedding-3-small',
            host: 'http://localhost:9292/v1',
          },
        },
      },
    };

    (useConfigStore as any).mockReturnValue({
      config: configWithHost,
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<MemoryConfig />);

    expect(screen.getByText('Base URL:')).toBeInTheDocument();
    expect(
      screen.getByText('http://localhost:9292/v1', { selector: '.font-mono' })
    ).toBeInTheDocument();
  });

  it('handles missing memory config gracefully', () => {
    (useConfigStore as any).mockReturnValue({
      config: {},
      updateMemoryConfig: mockUpdateMemoryConfig,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<MemoryConfig />);

    // Should show default values
    const providerSelect = document.getElementById('provider');
    expect(providerSelect).toHaveTextContent('OpenAI');
    const modelInput = document.getElementById('model') as HTMLInputElement;
    expect(modelInput).toHaveValue('text-embedding-3-small');
  });
});
