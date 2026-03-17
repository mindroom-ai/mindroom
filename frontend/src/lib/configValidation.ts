export interface ConfigValidationIssue {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

export interface GlobalConfigDiagnostic {
  kind: 'global';
  message: string;
  blocking: boolean;
}

export interface ValidationConfigDiagnostic {
  kind: 'validation';
  issue: ConfigValidationIssue;
}

export type ConfigDiagnostic = GlobalConfigDiagnostic | ValidationConfigDiagnostic;

export function getConfigValidationIssues(
  diagnostics: ConfigDiagnostic[]
): ConfigValidationIssue[] {
  return diagnostics
    .filter(
      (diagnostic): diagnostic is ValidationConfigDiagnostic => diagnostic.kind === 'validation'
    )
    .map(diagnostic => diagnostic.issue);
}

export function getGlobalConfigDiagnostics(
  diagnostics: ConfigDiagnostic[]
): GlobalConfigDiagnostic[] {
  const explicitGlobals = diagnostics.filter(
    (diagnostic): diagnostic is GlobalConfigDiagnostic => diagnostic.kind === 'global'
  );
  const rootValidationGlobals = getConfigValidationIssues(diagnostics)
    .filter(issue => issue.loc.length === 0)
    .map(issue => ({
      kind: 'global' as const,
      message: issue.msg,
      blocking: false,
    }));
  const seen = new Set<string>();

  return [...explicitGlobals, ...rootValidationGlobals].filter(diagnostic => {
    const key = `${diagnostic.blocking}:${diagnostic.message}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

export function findConfigValidationIssue(
  diagnostics: ConfigDiagnostic[],
  prefix: Array<string | number>,
  exact: boolean = false
): string | undefined {
  return getConfigValidationIssues(diagnostics).find(issue =>
    exact
      ? issue.loc.length === prefix.length &&
        prefix.every((segment, index) => issue.loc[index] === segment)
      : prefix.every((segment, index) => issue.loc[index] === segment)
  )?.msg;
}
