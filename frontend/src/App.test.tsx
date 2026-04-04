import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useConfigStore } from '@/store/configStore';

import App, { resolveCurrentTab, shouldShowBlockingDiagnosticOverlay } from './App';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

vi.mock('@/components/ui/toaster', () => ({
  toast: vi.fn(),
  Toaster: () => null,
}));

describe('resolveCurrentTab', () => {
  it('defaults to dashboard for empty and unknown paths', () => {
    expect(resolveCurrentTab('/')).toBe('dashboard');
    expect(resolveCurrentTab('/unknown')).toBe('dashboard');
  });

  it('ignores trailing and repeated slashes for known tabs', () => {
    expect(resolveCurrentTab('/dashboard/')).toBe('dashboard');
    expect(resolveCurrentTab('///agents//')).toBe('agents');
    expect(resolveCurrentTab('/teams/details')).toBe('teams');
  });
});

describe('shouldShowBlockingDiagnosticOverlay', () => {
  it('keeps access overlays blocking for auth failures', () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Authentication required. Please log in to access this instance.',
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false }
      )
    ).toBe(true);
  });

  it('keeps the dashboard visible when a draft or validation details make recovery possible', () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Network error',
          blocking: true,
        },
        { hasLoadedConfig: true, hasRecoveryConfig: false }
      )
    ).toBe(false);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: true,
        },
        { hasLoadedConfig: true, hasRecoveryConfig: false }
      )
    ).toBe(false);
  });

  it('keeps recovery mode visible for generic blocking failures when a recovery draft exists', () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Server error. Please try again later or contact support.',
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: true }
      )
    ).toBe(true);
  });

  it('still blocks generic failures when there is no recoverable config state', () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Server error. Please try again later or contact support.',
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false }
      )
    ).toBe(true);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: true,
        },
        { hasLoadedConfig: false, hasRecoveryConfig: false }
      )
    ).toBe(true);
  });
});

describe('App recovery mode', () => {
  const mockSaveRecoveryConfigSource = vi.fn();

  beforeEach(() => {
    mockSaveRecoveryConfigSource.mockReset();
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: vi.fn(() => 'system'),
        setItem: vi.fn(),
        removeItem: vi.fn(),
        clear: vi.fn(),
      },
    });
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
      }),
    });
    vi.mocked(useConfigStore).mockReturnValue({
      loadConfig: vi.fn(),
      config: null,
      recoveryConfigSource: 'agents:\n  broken: true\n',
      recoveryConfigSourceOriginal: 'agents:\n  broken: false\n',
      updateRecoveryConfigSource: vi.fn(),
      saveRecoveryConfigSource: mockSaveRecoveryConfigSource,
      syncStatus: 'error',
      diagnostics: [
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: true,
        },
      ],
      isLoading: false,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedCultureId: null,
      selectedRoomId: null,
    } as never);
  });

  it('renders the recovery editor when a blocking recovery draft exists', () => {
    render(<App />);

    expect(screen.getByRole('button', { name: 'Save Replacement Config' })).toBeInTheDocument();
    expect(
      screen.getByText(/could not be loaded\. edit the raw configuration/i)
    ).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('agents:\n  broken: true\n');
  });

  it('surfaces stale recovery save results', async () => {
    mockSaveRecoveryConfigSource.mockResolvedValue({ status: 'stale' });

    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: 'Save Replacement Config' }));

    const { toast } = await import('@/components/ui/toaster');
    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith({
        title: 'Save Failed',
        description: 'Save was superseded by newer recovery edits.',
        variant: 'destructive',
      });
    });
  });
});
