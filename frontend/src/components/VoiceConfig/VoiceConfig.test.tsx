import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { VoiceConfig } from './VoiceConfig';
import { useConfigStore } from '@/store/configStore';
import type { ConfigDiagnostic } from '@/lib/configValidation';
import { Config } from '@/types/config';
import type { SaveConfigResult } from '@/store/configStore';

vi.mock('@/store/configStore');

const { mockToast, mockToaster } = vi.hoisted(() => ({
  mockToast: vi.fn(),
  mockToaster: vi.fn(),
}));
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));
vi.mock('@/components/ui/toaster', () => ({
  toast: mockToaster,
}));

describe('VoiceConfig', () => {
  const mockSaveConfig = vi.fn();
  const mockUpdateVoiceConfig = vi.fn();
  type MockStoreState = {
    config: Config;
    diagnostics: ConfigDiagnostic[];
    syncStatus: 'synced' | 'syncing' | 'error' | 'disconnected';
    isDirty: boolean;
    isLoading: boolean;
    saveConfig: () => Promise<SaveConfigResult>;
    updateVoiceConfig: typeof mockUpdateVoiceConfig;
  };
  type MockedStoreHook = {
    (): MockStoreState;
    getState: () => MockStoreState;
    mockReturnValue: (value: MockStoreState) => void;
  };
  const mockedUseConfigStore = useConfigStore as unknown as MockedStoreHook;
  let mockStoreState: MockStoreState;

  const createConfig = (): Partial<Config> => ({
    models: {
      default: { provider: 'openai', id: 'gpt-4o-mini' },
      fast: { provider: 'openai', id: 'gpt-4.1-mini' },
    },
    voice: {
      enabled: true,
      visible_router_echo: false,
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
    mockStoreState = {
      config: config as Config,
      diagnostics: [],
      syncStatus: 'synced',
      isDirty: false,
      isLoading: false,
      saveConfig: mockSaveConfig,
      updateVoiceConfig: mockUpdateVoiceConfig,
    };
    mockedUseConfigStore.mockReturnValue(mockStoreState);
    mockedUseConfigStore.getState = vi.fn(() => mockStoreState);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = 'synced';
      mockStoreState.isDirty = false;
      return { status: 'saved' };
    });
    setMockStore(createConfig());
  });

  it('shows current effective settings summary', () => {
    render(<VoiceConfig />);

    expect(screen.getByText('Current Effective Settings')).toBeInTheDocument();
    expect(screen.getByText('OpenAI-compatible API')).toBeInTheDocument();
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:8080/v1/audio/transcriptions')).toBeInTheDocument();
    expect(screen.getByText('OPENAI_API_KEY environment variable')).toBeInTheDocument();
  });

  it('always shows optional base url input', () => {
    const config = createConfig();
    if (!config.voice) throw new Error('Expected voice config');
    config.voice.stt.provider = 'openai';

    setMockStore(config);

    render(<VoiceConfig />);

    const hostInput = document.getElementById('stt-base-url') as HTMLInputElement;
    expect(hostInput).toBeInTheDocument();
    expect(hostInput).toHaveValue('http://localhost:8080');
  });

  it('uses default openai endpoint when base url is cleared', async () => {
    render(<VoiceConfig />);

    const hostInput = document.getElementById('stt-base-url') as HTMLInputElement;
    fireEvent.change(hostInput, { target: { value: '' } });

    await waitFor(() => {
      expect(mockUpdateVoiceConfig).toHaveBeenCalledWith({
        enabled: true,
        visible_router_echo: false,
        stt: {
          provider: 'openai',
          model: 'whisper-1',
          host: '',
          api_key: '',
        },
        intelligence: {
          model: 'default',
        },
      });
      expect(
        screen.getByText('https://api.openai.com/v1/audio/transcriptions')
      ).toBeInTheDocument();
    });
  });

  it('normalizes and saves voice settings as openai provider', async () => {
    const config = createConfig();
    if (!config.voice) throw new Error('Expected voice config');
    config.voice.stt.host = 'http://localhost:8080/';
    setMockStore(config);

    render(<VoiceConfig />);

    const saveButton = screen.getByRole('button', { name: 'Save Voice Configuration' });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
      expect(mockUpdateVoiceConfig).toHaveBeenLastCalledWith({
        enabled: true,
        visible_router_echo: false,
        stt: {
          provider: 'openai',
          model: 'whisper-1',
          host: 'http://localhost:8080',
          api_key: '',
        },
        intelligence: {
          model: 'default',
        },
      });
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Voice Configuration Saved',
        description: 'Your voice settings have been updated successfully.',
      });
    });
  });

  it('shows an error toast when saving fails', async () => {
    mockSaveConfig.mockImplementation(async () => {
      mockStoreState.syncStatus = 'error';
      mockStoreState.isDirty = true;
      return {
        status: 'error',
        message: 'Configuration validation failed',
        diagnostics: mockStoreState.diagnostics,
      };
    });
    mockStoreState.diagnostics = [
      {
        kind: 'global',
        message: 'Configuration validation failed',
        blocking: false,
      },
    ];

    render(<VoiceConfig />);

    fireEvent.click(screen.getByRole('button', { name: 'Save Voice Configuration' }));

    await waitFor(() => {
      expect(mockToaster).toHaveBeenCalledWith({
        title: 'Save Failed',
        description: 'Configuration validation failed',
        variant: 'destructive',
      });
    });
  });

  it('shows a stale-save toast when a newer voice draft supersedes the request', async () => {
    mockSaveConfig.mockResolvedValueOnce({ status: 'stale' });

    render(<VoiceConfig />);

    fireEvent.click(screen.getByRole('button', { name: 'Save Voice Configuration' }));

    await waitFor(() => {
      expect(mockToaster).toHaveBeenCalledWith({
        title: 'Save Failed',
        description: 'Save was superseded by newer voice configuration edits.',
        variant: 'destructive',
      });
    });
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Voice Configuration Saved' })
    );
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

  it('updates visible router echo from the voice tab', async () => {
    const config = createConfig();
    setMockStore(config);

    render(<VoiceConfig />);

    const visibleRouterEchoToggle = document.getElementById(
      'visible-router-echo'
    ) as HTMLInputElement;
    fireEvent.click(visibleRouterEchoToggle);

    await waitFor(() => {
      expect(mockUpdateVoiceConfig).toHaveBeenCalledWith({
        enabled: true,
        visible_router_echo: true,
        stt: {
          provider: 'openai',
          model: 'whisper-1',
          host: 'http://localhost:8080',
          api_key: '',
        },
        intelligence: {
          model: 'default',
        },
      });
      expect(screen.getByText('Visible Router Echo:')).toBeInTheDocument();
      expect(visibleRouterEchoToggle).toBeChecked();
    });
  });

  it('disables save while a save is already in progress', () => {
    const config = createConfig();
    setMockStore(config);
    mockStoreState.isLoading = true;

    render(<VoiceConfig />);

    expect(screen.getByRole('button', { name: 'Save Voice Configuration' })).toBeDisabled();
  });
});
