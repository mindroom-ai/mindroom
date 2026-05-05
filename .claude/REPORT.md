# Matrix Delivery Policy Failure Handling

## Summary of changes

Added a typed `MatrixExpectedDeliveryPolicyError` for deterministic Matrix delivery policy rejections raised from nio trust checks.
Kept Matrix delivery logs sanitized and compact while preserving traceback propagation for genuinely unexpected local delivery exceptions.
Updated streaming delivery to stop retrying terminal updates when the failure is an expected Matrix delivery policy rejection.
Updated response-level streaming logging to emit compact structured warnings for expected policy rejections without traceback rendering.
Added focused tests covering typed policy failures, compact logging, and skipped terminal retry behavior.

## Tests run and results

`uv run ruff check src/mindroom/matrix/client_delivery.py src/mindroom/matrix/client.py src/mindroom/streaming.py src/mindroom/response_runner.py tests/test_send_file_message.py tests/test_streaming_behavior.py` passed.
`uv run ruff format --check src/mindroom/matrix/client_delivery.py src/mindroom/matrix/client.py src/mindroom/streaming.py src/mindroom/response_runner.py tests/test_send_file_message.py tests/test_streaming_behavior.py` passed.
`uv run --python 3.13 pytest tests/test_send_file_message.py tests/test_streaming_behavior.py tests/test_tach_split_matrix_client_boundaries.py::test_split_matrix_client_importers_have_explicit_tach_modules -q -n 0 --no-cov` passed with 126 tests.
`uv run --python 3.13 pytest --no-cov` passed with 6248 tests passed and 56 skipped.
An earlier full parallel pytest run exposed timing-sensitive approval and compaction failures plus a split-client Tach boundary failure.
The Tach boundary issue was fixed in this patch, the timing-sensitive failures passed when rerun serially, and the final full parallel pytest run passed.

## Remaining risks/questions

The policy classification currently covers `MatrixExpectedDeliveryPolicyError` and nio `OlmTrustError` subclasses.
If nio adds new deterministic trust-policy exception types outside that hierarchy, the classifier should be extended.
Unexpected Matrix delivery exceptions now propagate after compact low-level logging so higher layers can preserve tracebacks.

## Suggested PR title

Handle expected Matrix delivery policy rejections compactly

## Suggested PR body

### Summary

- Classify deterministic Matrix delivery policy rejections with a typed exception.
- Log expected Matrix policy delivery failures without expensive traceback rendering at streaming and response boundaries.
- Skip terminal streaming retries when the underlying failure is a deterministic policy rejection.
- Preserve tracebacks for unexpected Matrix delivery exceptions.

### Tests

- `uv run ruff check src/mindroom/matrix/client_delivery.py src/mindroom/matrix/client.py src/mindroom/streaming.py src/mindroom/response_runner.py tests/test_send_file_message.py tests/test_streaming_behavior.py`
- `uv run ruff format --check src/mindroom/matrix/client_delivery.py src/mindroom/matrix/client.py src/mindroom/streaming.py src/mindroom/response_runner.py tests/test_send_file_message.py tests/test_streaming_behavior.py`
- `uv run --python 3.13 pytest tests/test_send_file_message.py tests/test_streaming_behavior.py tests/test_tach_split_matrix_client_boundaries.py::test_split_matrix_client_importers_have_explicit_tach_modules -q -n 0 --no-cov`
- `uv run --python 3.13 pytest --no-cov`
