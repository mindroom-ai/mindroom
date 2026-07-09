// Mirrors the backend predicate in src/mindroom/matrix_identifiers.py
// (_is_concrete_matrix_user_id): keep the two definitions in sync so the UI
// only accepts entries the backend will actually apply.
export function isConcreteMatrixUserId(userId: string): boolean {
  return (
    userId.startsWith("@") &&
    userId.includes(":") &&
    !userId.includes("*") &&
    !userId.includes("?") &&
    !userId.includes(" ")
  );
}
