# Tool-call overhead

The benchmark separates tool execution overhead from model latency and the surrounding conversation pipeline.
It executes harmless commands directly; it does not send Matrix messages or call a model.

## Reproduce

Run from the checkout whose runtime you want to measure:

```bash
uv run scripts/testing/benchmark_tool_call_overhead.py --iterations 1000 --warmup 50
uv run scripts/testing/benchmark_tool_call_overhead.py --shell --iterations 100 --warmup 5
uv run scripts/testing/benchmark_tool_call_overhead.py --docker-image YOUR_LOCAL_IMAGE --iterations 30 --warmup 3
```

The default mode measures the hook bridge with trivial synchronous and asynchronous functions.
The shell mode measures the real local shell toolkit with argv and command-string inputs.
The Docker mode requires Docker daemon access and a locally available MindRoom image matching the checkout.
It creates an isolated config, storage directory, token, and worker with no granted credentials, then removes its containers on exit.
It includes worker readiness checks and authenticated HTTP execution, but excludes agent construction, plugin hooks, model calls, and conversation loading in the parent.
The command is `printf benchmark-ok`; Docker uses argv input so login-shell startup does not distort the worker comparison.

Docker results separate warm worker ensure, execution, and their combined latency.
`docker_cold_ready` measures initial container readiness, not first-tool/template initialization; warmup calls exclude that initialization from warm results.
For import-level profiling, run the script with `uv run python -m cProfile -o tool-call.prof` before the script path and arguments.
Parent profiling does not capture code inside the Docker request child.
Do not compare profiled timings directly with unprofiled timings.

## Measured bottlenecks and changes

Measurements on the development host in September 2026 identified three avoidable costs:

- Implicit command strings started login Bash and repeatedly loaded host profiles; use non-login Bash with the prepared environment, retaining explicit `bash -lc` support.
- A warm Docker ensure resolved the same image three times; pass one request-local image snapshot through validation, resolving again on the next call so retags still take effect.
- Agno tool-schema construction imported `Agent` and `Team` afresh in each fork child; preload their class definitions in the single-threaded template, without constructing agents or sharing request state.

An unprofiled paired comparison against `f1be0a562`, using the same host and Docker base image/dependencies, measured:

| Warm Docker phase | Baseline median | Optimized median |
| --- | ---: | ---: |
| Ensure | 113.5 ms | 68.0 ms |
| Execute | 228.6 ms | 76.5 ms |
| Combined | 340.3 ms | 144.5 ms |

Combined p95 fell from 407.7 ms to 166.0 ms across 30 measured calls per version, after three warmup calls.
Local command strings fell from 268.6 ms to 3.2 ms median across 100 measured calls after five warmups; direct argv stayed around 1.3–1.4 ms.
These are host-dependent measurements, not latency guarantees or CI thresholds.
Earlier instrumented profiling runs were slower and are not used for this comparison.
Authorization, credential preparation, worker isolation, and per-call image/config validation remain in place.
Remaining Docker time includes daemon round trips, filesystem/config work, HTTP setup, and fresh request-child execution.
