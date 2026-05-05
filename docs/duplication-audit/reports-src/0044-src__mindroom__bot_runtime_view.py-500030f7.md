Summary: No meaningful duplication found.
`src/mindroom/bot_runtime_view.py` defines a narrow protocol plus one mutable dataclass that centralizes live bot runtime state for extracted collaborators.
The closest related code is `AgentBot` property forwarding in `src/mindroom/bot.py` and narrower protocol definitions in `src/mindroom/runtime_protocols.py`, but those serve different boundaries rather than duplicating reusable behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
BotRuntimeView	class	lines 25-53	related-only	BotRuntimeView RuntimeView Protocol runtime state collaborator surface	src/mindroom/runtime_protocols.py:76 src/mindroom/runtime_protocols.py:83 src/mindroom/runtime_protocols.py:93 src/mindroom/runtime_protocols.py:100 src/mindroom/matrix/conversation_cache.py:354 src/mindroom/response_runner.py:347 src/mindroom/turn_controller.py:178
BotRuntimeView.client	method	lines 29-29	related-only	def client client property runtime protocol bot forwarding	src/mindroom/bot.py:499 src/mindroom/runtime_protocols.py:86
BotRuntimeView.config	method	lines 32-32	related-only	def config config property runtime protocol bot forwarding	src/mindroom/bot.py:507 src/mindroom/runtime_protocols.py:79 src/mindroom/runtime_protocols.py:89
BotRuntimeView.runtime_paths	method	lines 35-35	related-only	def runtime_paths runtime_paths property runtime protocol bot field	src/mindroom/bot.py:294 src/mindroom/runtime_protocols.py:41
BotRuntimeView.enable_streaming	method	lines 38-38	related-only	def enable_streaming enable_streaming property bot forwarding presence	src/mindroom/bot.py:518 src/mindroom/matrix/presence.py:177
BotRuntimeView.orchestrator	method	lines 41-41	related-only	def orchestrator orchestrator property runtime protocol bot forwarding	src/mindroom/bot.py:527 src/mindroom/runtime_protocols.py:96 src/mindroom/runtime_protocols.py:103
BotRuntimeView.event_cache	method	lines 44-44	related-only	def event_cache ConversationEventCache runtime cache protocol bot forwarding	src/mindroom/bot.py:537 src/mindroom/matrix/conversation_cache.py:359 src/mindroom/matrix/cache/thread_write_cache_ops.py:37
BotRuntimeView.event_cache_write_coordinator	method	lines 47-47	related-only	def event_cache_write_coordinator write coordinator runtime property	src/mindroom/bot.py:552 src/mindroom/runtime_support.py:69 src/mindroom/matrix/cache/thread_write_cache_ops.py:84 src/mindroom/matrix/cache/thread_reads.py:66
BotRuntimeView.startup_thread_prewarm_registry	method	lines 50-50	related-only	def startup_thread_prewarm_registry prewarm registry runtime property	src/mindroom/bot.py:566 src/mindroom/runtime_support.py:70 src/mindroom/runtime_support.py:143
BotRuntimeView.runtime_started_at	method	lines 53-53	related-only	def runtime_started_at runtime_started_at property protocol snapshot freshness	src/mindroom/bot.py:580 src/mindroom/runtime_protocols.py:106 src/mindroom/hooks/context.py:206 src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:84 src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:88
BotRuntimeState	class	lines 57-72	related-only	BotRuntimeState dataclass runtime state live mutable event_cache startup registry	src/mindroom/bot.py:307 src/mindroom/runtime_support.py:64 src/mindroom/api/config_lifecycle.py:70 src/mindroom/runtime_state.py:9
BotRuntimeState.mark_runtime_started	method	lines 70-72	none-found	mark_runtime_started mark started_at time.time runtime start timestamp	none
```

Findings:

No real duplicated behavior was found.
`AgentBot` forwards `client`, `config`, `enable_streaming`, `orchestrator`, `event_cache`, `event_cache_write_coordinator`, `startup_thread_prewarm_registry`, and `runtime_started_at` to `BotRuntimeState` in `src/mindroom/bot.py:499`, `src/mindroom/bot.py:507`, `src/mindroom/bot.py:518`, `src/mindroom/bot.py:527`, `src/mindroom/bot.py:537`, `src/mindroom/bot.py:552`, `src/mindroom/bot.py:566`, and `src/mindroom/bot.py:580`.
Those accessors are an adapter from the historical `AgentBot` public surface to the extracted runtime-state object, not an independent implementation of the same behavior.
The accessors for `event_cache`, `event_cache_write_coordinator`, and `startup_thread_prewarm_registry` also add startup-injection guard errors that are intentionally absent from the raw dataclass fields.

`src/mindroom/runtime_protocols.py:76`, `src/mindroom/runtime_protocols.py:83`, `src/mindroom/runtime_protocols.py:93`, and `src/mindroom/runtime_protocols.py:100` define smaller structural protocols that overlap parts of `BotRuntimeView`.
This is related type-surface duplication, but `BotRuntimeView` already contains a type-only compatibility proof at `src/mindroom/bot_runtime_view.py:77` to keep those narrower protocols aligned.
The narrower protocols are used by collaborators that require less than the full bot runtime surface, so collapsing them would broaden dependencies rather than reduce active behavioral duplication.

`OwnedRuntimeSupport` in `src/mindroom/runtime_support.py:64` carries the concrete cache support objects that later get injected into `BotRuntimeState`.
It overlaps three field names, but it models ownership and lifecycle of shared services, while `BotRuntimeState` models one bot's mutable live view.
The difference is meaningful because `OwnedRuntimeSupport` also carries `event_cache_identity` and owns close/rebuild behavior in `src/mindroom/runtime_support.py:241`.

Proposed generalization: No refactor recommended.
The existing split is small and purposeful: `BotRuntimeState` is the mutable backing object, `BotRuntimeView` is the collaborator contract, `AgentBot` preserves its property API, and `runtime_protocols.py` keeps narrower dependency surfaces.

Risk/tests:

No production changes were made.
If future refactoring touches this area, tests should cover startup support injection failures in `AgentBot.event_cache`, `AgentBot.event_cache_write_coordinator`, and `AgentBot.startup_thread_prewarm_registry`, plus type-check coverage for collaborators that accept `BotRuntimeView` or the narrower runtime protocols.
