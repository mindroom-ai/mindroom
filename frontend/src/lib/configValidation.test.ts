import { describe, expect, it } from 'vitest';
import { getGlobalConfigDiagnostics, type ConfigDiagnostic } from './configValidation';

describe('getGlobalConfigDiagnostics', () => {
  it('includes root-level validation issues in the global diagnostics banner model', () => {
    const diagnostics: ConfigDiagnostic[] = [
      {
        kind: 'global',
        message: 'Configuration validation failed',
        blocking: false,
      },
      {
        kind: 'validation',
        issue: {
          loc: [],
          msg: 'At least one model must be configured.',
          type: 'value_error',
        },
      },
      {
        kind: 'validation',
        issue: {
          loc: ['agents', 'mind', 'role'],
          msg: 'Role must not be blank.',
          type: 'value_error',
        },
      },
    ];

    expect(getGlobalConfigDiagnostics(diagnostics)).toEqual([
      {
        kind: 'global',
        message: 'Configuration validation failed',
        blocking: false,
      },
      {
        kind: 'global',
        message: 'At least one model must be configured.',
        blocking: false,
      },
    ]);
  });
});
