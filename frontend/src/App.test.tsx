import { describe, expect, it } from 'vitest';

import { resolveCurrentTab, shouldShowBlockingDiagnosticOverlay } from './App';

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
        { hasLoadedConfig: false }
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
        { hasLoadedConfig: true }
      )
    ).toBe(false);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: true,
        },
        { hasLoadedConfig: true }
      )
    ).toBe(false);
  });

  it('still blocks generic failures when there is no recoverable config state', () => {
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Server error. Please try again later or contact support.',
          blocking: true,
        },
        { hasLoadedConfig: false }
      )
    ).toBe(true);
    expect(
      shouldShowBlockingDiagnosticOverlay(
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: true,
        },
        { hasLoadedConfig: false }
      )
    ).toBe(true);
  });
});
