// Mirrors the backend predicate in src/mindroom/matrix_identifiers.py
// (_is_concrete_matrix_user_id): keep the two definitions in sync so the UI
// only accepts entries the backend will actually apply.
export function isConcreteMatrixUserId(userId: string): boolean {
  if (!userId.startsWith("@") || userId.includes("*") || userId.includes("?")) {
    return false;
  }
  if (/\s/.test(userId)) {
    return false;
  }
  const rest = userId.slice(1);
  const separatorIndex = rest.indexOf(":");
  return separatorIndex > 0 && separatorIndex < rest.length - 1;
}
