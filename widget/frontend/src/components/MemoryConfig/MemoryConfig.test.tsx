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
        provider: 'ollama',
        config: {
          model: 'nomic-embed-text',
          host: 'http://localhost:11434',
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
    expect(screen.getByText('ollama', { selector: '.font-mono' })).toBeInTheDocument();
    expect(screen.getByText('nomic-embed-text', { selector: '.font-mono' })).toBeInTheDocument();
    expect(
      screen.getByText('http://localhost:11434', { selector: '.font-mono' })
    ).toBeInTheDocument();
  });

  it('shows correct provider in select', () => {
    render(<MemoryConfig />);

    // For Radix UI Select, we need to find the trigger button by its role
    const providerSelect = document.getElementById('provider');
    expect(providerSelect).toBeInTheDocument();
    expect(providerSelect).toHaveTextContent('Ollama (Local)');
  });

  it('shows correct model in select', () => {
    render(<MemoryConfig />);

    const modelSelect = document.getElementById('model');
    expect(modelSelect).toBeInTheDocument();
    expect(modelSelect).toHaveTextContent('nomic-embed-text');
  });

  it('shows host input for ollama provider', () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById('host') as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue('http://localhost:11434');
  });

  it('changes provider and updates model options', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    expect(providerSelect).toBeInTheDocument();
    fireEvent.click(providerSelect!);

    const openaiOption = await screen.findByText('OpenAI');
    fireEvent.click(openaiOption);

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith({
        provider: 'openai',
        model: 'text-embedding-ada-002',
        host: 'http://localhost:11434',
      });
    });
  });

  it('shows API key notice for OpenAI', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);

    const openaiOption = await screen.findByText('OpenAI');
    fireEvent.click(openaiOption);

    await waitFor(() => {
      expect(screen.getByText(/OPENAI_API_KEY/)).toBeInTheDocument();
    });
  });

  it('shows API key notice for HuggingFace', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);

    const hfOption = await screen.findByText('HuggingFace');
    fireEvent.click(hfOption);

    await waitFor(() => {
      expect(screen.getByText(/HUGGINGFACE_API_KEY/)).toBeInTheDocument();
    });
  });

  it('does not show host input for non-ollama providers', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);

    const openaiOption = await screen.findByText('OpenAI');
    fireEvent.click(openaiOption);

    await waitFor(() => {
      const hostInput = document.getElementById('host');
      expect(hostInput).not.toBeInTheDocument();
    });
  });

  it('shows correct model options for ollama', async () => {
    render(<MemoryConfig />);

    const modelSelect = document.getElementById('model');
    fireEvent.click(modelSelect!);

    // Wait for options to appear
    await waitFor(() => {
      const nomicOptions = screen.getAllByText('nomic-embed-text');
      expect(nomicOptions.length).toBeGreaterThan(0);
    });

    // Check other options exist
    const minilmOptions = screen.getAllByText('all-minilm');
    expect(minilmOptions.length).toBeGreaterThan(0);
    const mxbaiOptions = screen.getAllByText('mxbai-embed-large');
    expect(mxbaiOptions.length).toBeGreaterThan(0);
  });

  it('shows correct model options for openai', async () => {
    // First switch to OpenAI
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);
    const openaiOptions = await screen.findAllByText('OpenAI');
    fireEvent.click(openaiOptions[0]);

    // Then check model options
    const modelSelect = document.getElementById('model');
    fireEvent.click(modelSelect!);

    // Wait for options and check they exist
    await waitFor(() => {
      const adaOptions = screen.getAllByText('text-embedding-ada-002');
      expect(adaOptions.length).toBeGreaterThan(0);
    });

    const smallOptions = screen.getAllByText('text-embedding-3-small');
    expect(smallOptions.length).toBeGreaterThan(0);
    const largeOptions = screen.getAllByText('text-embedding-3-large');
    expect(largeOptions.length).toBeGreaterThan(0);
  });

  it('updates model when selection changes', async () => {
    render(<MemoryConfig />);

    const modelSelect = document.getElementById('model');
    fireEvent.click(modelSelect!);

    const newModel = await screen.findByText('all-minilm');
    fireEvent.click(newModel);

    expect(mockUpdateMemoryConfig).toHaveBeenCalledWith({
      provider: 'ollama',
      model: 'all-minilm',
      host: 'http://localhost:11434',
    });
  });

  it('updates host when input changes', async () => {
    render(<MemoryConfig />);

    const hostInput = document.getElementById('host') as HTMLInputElement;
    fireEvent.change(hostInput, { target: { value: 'http://localhost:8000' } });

    await waitFor(() => {
      expect(mockUpdateMemoryConfig).toHaveBeenCalledWith({
        provider: 'ollama',
        model: 'nomic-embed-text',
        host: 'http://localhost:8000',
      });
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

  it('shows provider description for ollama', () => {
    render(<MemoryConfig />);

    expect(screen.getByText('Local embeddings using Ollama')).toBeInTheDocument();
  });

  it('shows provider description for openai', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);
    const openaiOption = await screen.findByText('OpenAI');
    fireEvent.click(openaiOption);

    await waitFor(() => {
      expect(screen.getByText('Cloud embeddings using OpenAI API')).toBeInTheDocument();
    });
  });

  it('shows provider description for sentence-transformers', async () => {
    render(<MemoryConfig />);

    const providerSelect = document.getElementById('provider');
    fireEvent.click(providerSelect!);
    const stOption = await screen.findByText('Sentence Transformers');
    fireEvent.click(stOption);

    await waitFor(() => {
      expect(screen.getByText('Local embeddings using sentence-transformers')).toBeInTheDocument();
    });
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
    expect(providerSelect).toHaveTextContent('Ollama (Local)');
    const modelSelect = document.getElementById('model');
    expect(modelSelect).toHaveTextContent('nomic-embed-text');
  });
});
