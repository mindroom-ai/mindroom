import { describe, expect, it } from 'vitest';

import { resolveCurrentTab } from './App';

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
