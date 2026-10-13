# Tool-call overhead

This records a benchmark and September 2026 measurements that separate tool execution overhead from model latency and the surrounding conversation pipeline.
The benchmark executes harmless commands directly; it does not send Matrix messages or call a model.

## Reproduce

Run from the checkout whose runtime you want to measure:

```bash
uv run scripts/testing/benchmark_tool_call_overhead.py --iterations 1000 --warmup 50
uv run scripts/testing/benchmark_tool_call_overhead.py --shell --iterations 100 --warmup 5
uv run scripts/testing/benchmark_tool_call_overhead.py --docker-image YOUR_LOCAL_IMAGE --iterations 30 --warmup 3
```

- The Docker mode requires Docker daemon access and a locally available MindRoom image matching the checkout.
- Docker timings include worker readiness checks and authenticated HTTP execution, and exclude agent construction, plugin hooks, model calls, and conversation loading in the parent.
- Docker runs `printf benchmark-ok` as argv input so login-shell startup does not distort the worker comparison.
- `docker_cold_ready` measures initial container readiness, not first-tool or template initialization; warmup calls keep that initialization out of the warm results.
- For import-level profiling, put `uv run python -m cProfile -o tool-call.prof` before the script path and arguments.
- Parent profiling does not capture code inside the Docker request child, and profiled timings must not be compared directly with unprofiled timings.

## Bottlenecks found

Measurements on the development host in September 2026 identified three avoidable costs:

- Implicit command strings started login Bash, which repeatedly loaded host profiles.
- A warm Docker ensure resolved the same image three times; the fix still resolves the image again on each call so retags take effect.
- Agno tool-schema construction imported `Agent` and `Team` afresh in each fork child.

## Evidence

An unprofiled paired comparison against `f1be0a562`, using the same host and Docker base image and dependencies, measured:

| Warm Docker phase | Baseline median | Optimized median |
| --- | ---: | ---: |
| Ensure | 113.5 ms | 68.0 ms |
| Execute | 228.6 ms | 76.5 ms |
| Combined | 340.3 ms | 144.5 ms |

Combined p95 fell from 407.7 ms to 166.0 ms across 30 measured calls per version, after three warmup calls.
Local command strings fell from 268.6 ms to 3.2 ms median across 100 measured calls after five warmups; direct argv stayed around 1.3–1.4 ms.
The optimized timings still include authorization, credential preparation, worker isolation, and per-call image and config validation.
These are host-dependent measurements, not latency guarantees or CI thresholds.
Earlier instrumented profiling runs were slower and are not used for this comparison.
