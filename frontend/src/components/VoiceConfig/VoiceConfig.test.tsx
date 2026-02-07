import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { VoiceConfig } from './VoiceConfig';
import { useConfigStore } from '@/store/configStore';
import { Config } from '@/types/config';

vi.mock('@/store/configStore');

const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

describe('VoiceConfig', () => {
  const mockSaveConfig = vi.fn();
  const mockMarkDirty = vi.fn();
  type StoreState = ReturnType<typeof useConfigStore>;

  const createConfig = (): Partial<Config> => ({
    models: {
      default: { provider: 'openai', id: 'gpt-4o-mini' },
      fast: { provider: 'openai', id: 'gpt-4.1-mini' },
    },
    voice: {
      enabled: true,
      stt: {
        provider: 'custom',
        model: 'whisper-1',
        host: 'http://localhost:8080',
        api_key: '',
      },
      intelligence: {
        model: 'default',
      },
    },
  });

  const setMockStore = (config: Partial<Config>) => {
    vi.mocked(useConfigStore).mockReturnValue({
      config: config as Config,
      saveConfig: mockSaveConfig,
      markDirty: mockMarkDirty,
    } as unknown as StoreState);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    setMockStore(createConfig());
  });

  it('shows current effective settings summary', () => {
    render(<VoiceConfig />);

    expect(screen.getByText('Current Effective Settings')).toBeInTheDocument();
    expect(screen.getByText('OpenAI-compatible API')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:8080/v1/audio/transcriptions')).toBeInTheDocument();
    expect(screen.getByText('OPENAI_API_KEY environment variable')).toBeInTheDocument();
  });

  it('shows host input when host exists even with openai provider', () => {
    const config = createConfig();
    if (!config.voice) throw new Error('Expected voice config');
    config.voice.stt.provider = 'openai';

    setMockStore(config);

    render(<VoiceConfig />);

    const hostInput = document.getElementById('stt-host') as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue('http://localhost:8080');
  });

  it('clears host when switching provider to openai', async () => {
    const config = createConfig();

    setMockStore(config);

    render(<VoiceConfig />);

    const providerSelect = document.getElementById('stt-provider');
    expect(providerSelect).toBeInTheDocument();
    fireEvent.click(providerSelect!);

    const openAiOption = await screen.findByText('OpenAI API (Cloud)');
    fireEvent.click(openAiOption);

    await waitFor(() => {
      expect(mockMarkDirty).toHaveBeenCalled();
      expect(
        screen.getByText('https://api.openai.com/v1/audio/transcriptions')
      ).toBeInTheDocument();
    });
  });

  it('shows save button even when voice is disabled', async () => {
    const config = createConfig();
    if (!config.voice) throw new Error('Expected voice config');
    config.voice.enabled = false;

    setMockStore(config);

    render(<VoiceConfig />);

    const saveButton = screen.getByRole('button', { name: 'Save Voice Configuration' });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });
});
