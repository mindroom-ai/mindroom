# MyPy Type Checking Configuration for Tests

## Summary

We've configured MyPy to be pragmatic about test type checking while maintaining type safety in the main codebase. The test suite now has specific error codes disabled that are common and acceptable in test environments.

## Current Configuration

The `pyproject.toml` file now includes the following MyPy overrides for tests:

```toml
[[tool.mypy.overrides]]
module = "tests.*"
ignore_missing_imports = true
disable_error_code = [
  "no-untyped-def",     # Test functions often don't need full typing
  "arg-type",           # Mock objects often have incompatible types
  "assignment",         # Test fixtures often use mocks instead of real types
  "attr-defined",       # Mocks dynamically add attributes
  "unused-ignore",      # Some type ignores are needed conditionally
  "dict-item",          # Test configs use dicts instead of dataclasses
  "list-item",          # Test data often uses simplified types
  "union-attr",         # Test error handling checks multiple types
  "var-annotated",      # Test variables don't always need annotations
  "return-value",       # Mock return values may differ from real types
  "no-any-return",      # Mock functions may return Any
  "operator",           # Test assertions may check optional types
  "index",              # Tests check various response types
  "type-var",           # Tests may use complex type variables
  "comparison-overlap", # Tests may check impossible conditions for coverage
]
```

## Rationale for Each Disabled Error Code

### 1. `no-untyped-def`
Test functions and fixtures often don't benefit from full type annotations, especially when using pytest's dynamic fixture injection system.

### 2. `arg-type`
Mock objects (MagicMock, AsyncMock) are frequently passed where real types are expected. This is fundamental to unit testing with mocks.

### 3. `assignment`
Test fixtures often assign mock objects to variables that expect real types. This allows for isolated testing without real dependencies.

### 4. `attr-defined`
Mock objects dynamically add attributes like `assert_called_once()`, `call_args`, etc. that aren't present on the real types they're mocking.

### 5. `unused-ignore`
Some type ignores are conditionally needed depending on the test scenario. Rather than adding complex logic, we allow unused ignores.

### 6. `dict-item`
Tests frequently use dictionaries to simulate configuration loading from YAML/JSON, which would normally be dataclasses in production code.

### 7. `list-item`
Test data often uses simplified types (e.g., strings instead of MatrixID objects) for easier test writing and maintenance.

### 8. `union-attr`
Tests that handle error cases need to access attributes on union types to verify both success and failure paths.

### 9. `var-annotated`
Test variables, especially intermediate values in test setup, don't always need explicit type annotations.

### 10. `return-value`
Mock functions may return different types than the real functions they're replacing, especially when simulating error conditions.

### 11. `no-any-return`
Mock functions often return `Any` type, which is acceptable in test contexts where we're controlling the mock behavior.

### 12. `operator`
Tests may perform operations on optional types (like checking if a substring is in a potentially None transcription) to test error handling.

### 13. `index`
Tests need to access various response types (dict, str, None) without extensive type narrowing, especially when testing error paths.

### 14. `type-var`
Tests may use complex type variables in ways that don't strictly match the type system's expectations, particularly with sorted() and filter() operations.

### 15. `comparison-overlap`
Tests intentionally check conditions that might be impossible in production to ensure complete code coverage and error handling.

## Philosophy

This configuration represents a pragmatic balance between:

1. **Type Safety**: The main codebase remains strictly typed
2. **Test Flexibility**: Tests can use mocks and dynamic patterns effectively
3. **Maintainability**: Tests remain readable and easy to write
4. **Practicality**: We avoid fighting the type system in scenarios where it doesn't add value

## Migration Path

For new tests or when refactoring existing tests, consider:

1. Using proper type annotations where they add clarity
2. Creating typed test fixtures when the types are stable
3. Using `typing.cast()` when you know the type but MyPy doesn't
4. Adding specific `# type: ignore[code]` comments for one-off issues rather than relying on blanket disables

## Conclusion

This configuration allows the test suite to leverage Python's dynamic features and mocking capabilities while maintaining type safety where it matters most - in the production code. The disabled error codes are specifically chosen to address common and acceptable patterns in test code without compromising the overall type safety of the project.
