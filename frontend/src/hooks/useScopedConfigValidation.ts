import { useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { findConfigValidationIssue } from '@/lib/configValidation';

type ConfigValidationPath = Array<string | number>;

export function useScopedConfigValidation(prefix: ConfigValidationPath | null) {
  const { configValidationIssues } = useConfigStore();

  return useCallback(
    (path: ConfigValidationPath, exact: boolean = false): string | undefined => {
      if (prefix == null) {
        return undefined;
      }
      return findConfigValidationIssue(configValidationIssues, [...prefix, ...path], exact);
    },
    [configValidationIssues, prefix]
  );
}
