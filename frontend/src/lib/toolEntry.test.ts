import { describe, expect, it } from 'vitest';

import {
  cloneToolEntries,
  getToolOverrides,
  normalizeToolEntries,
  rebuildToolEntries,
  setToolOverridesInEntries,
  type ToolEntry,
} from './toolEntry';

describe('toolEntry', () => {
  it('normalizes mixed raw entries to tool names', () => {
    const rawEntries: ToolEntry[] = [
      'browser',
      { shell: { extra_env_passthrough: ['GITEA_TOKEN'] } },
      { name: 'coding', overrides: { mode: 'strict' } },
    ];

    expect(normalizeToolEntries(rawEntries)).toEqual(['browser', 'shell', 'coding']);
  });

  it('reads structured tool overrides', () => {
    const rawEntries: ToolEntry[] = [
      'browser',
      {
        shell: {
          extra_env_passthrough: ['GITEA_TOKEN'],
          shell_path_prepend: ['/run/wrappers/bin'],
        },
      },
    ];

    expect(getToolOverrides('browser', rawEntries)).toBeNull();
    expect(getToolOverrides('shell', rawEntries)).toEqual({
      extra_env_passthrough: ['GITEA_TOKEN'],
      shell_path_prepend: ['/run/wrappers/bin'],
    });
  });

  it('sets structured overrides without dropping unknown keys', () => {
    const rawEntries: ToolEntry[] = [
      {
        shell: {
          extra_env_passthrough: ['GITEA_TOKEN'],
          future_field: 'keep-me',
        },
      },
      'browser',
    ];

    expect(
      setToolOverridesInEntries(
        'shell',
        {
          extra_env_passthrough: null,
          shell_path_prepend: ['/run/wrappers/bin'],
        },
        rawEntries
      )
    ).toEqual([
      {
        shell: {
          future_field: 'keep-me',
          shell_path_prepend: ['/run/wrappers/bin'],
        },
      },
      'browser',
    ]);
  });

  it('collapses a structured entry back to a plain string when overrides are cleared', () => {
    const rawEntries: ToolEntry[] = [
      { shell: { extra_env_passthrough: ['GITEA_TOKEN'] } },
      'browser',
    ];

    expect(
      setToolOverridesInEntries(
        'shell',
        {
          extra_env_passthrough: [],
        },
        rawEntries
      )
    ).toEqual(['shell', 'browser']);
  });

  it('rebuilds the selected tool list using preserved raw entries', () => {
    const rawEntries: ToolEntry[] = [
      { shell: { extra_env_passthrough: ['GITEA_TOKEN'] } },
      'browser',
    ];

    expect(rebuildToolEntries(['browser', 'shell', 'coding'], rawEntries)).toEqual([
      'browser',
      { shell: { extra_env_passthrough: ['GITEA_TOKEN'] } },
      'coding',
    ]);
  });

  it('clones tool entries defensively', () => {
    const rawEntries: ToolEntry[] = [{ shell: { extra_env_passthrough: ['GITEA_TOKEN'] } }];
    const clonedEntries = cloneToolEntries(rawEntries);

    const shellEntry = clonedEntries[0] as Record<string, { extra_env_passthrough: string[] }>;
    shellEntry.shell.extra_env_passthrough.push('WHISPER_URL');

    expect(rawEntries).toEqual([{ shell: { extra_env_passthrough: ['GITEA_TOKEN'] } }]);
  });
});
