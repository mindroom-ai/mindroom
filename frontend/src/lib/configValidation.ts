export interface ConfigValidationIssue {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

export function findConfigValidationIssue(
  issues: ConfigValidationIssue[],
  prefix: Array<string | number>,
  exact: boolean = false
): string | undefined {
  return issues.find(issue =>
    exact
      ? issue.loc.length === prefix.length &&
        prefix.every((segment, index) => issue.loc[index] === segment)
      : prefix.every((segment, index) => issue.loc[index] === segment)
  )?.msg;
}
