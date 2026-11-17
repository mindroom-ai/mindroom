# Subagent Cost on Replies

This record covers how a reply reports what its subagents cost, and what was rejected.

## Goal

A reply's cost covers only its own agent's model requests, so a turn that delegates most of its work to subagents looks cheap.
Each reply should also report the cost of the subagent turns whose results it received, and the thread total should include them.
Success: a reply that ran a `run_subagent` reviewer shows its own cost and the reviewer's cost, and the thread total adds both.

## Prior art

- `build_ai_run_metadata_content` (`ai_run_metadata.py`) prices a reply's turn with `run_cost_usd`, which prices each model of `RunMetrics.details` at its own price, and publishes `cost.usd`.
- A settled child's usage goes into its delegation record (`child_response_usage`, `delegation/audit.py`) as tokens only. Nothing prices it.
- Child runs record their usage in the usage ledger, so the budget scan already counts their spend.

## Decisions

1. A reply's `io.mindroom.ai_run` `cost` gains `subagents_usd`, beside `usd`, in the same micro-dollar string format.
   `usd` stays the reply's own cost, so it still matches the reply's own `usage` and context.
   The key is left out when the turn received no priced subagent cost.
2. A subagent turn's cost is counted on the reply whose turn received its result.
   For `run_subagent` and `continue_subagent`, that is the reply that called the tool.
   For a background child, that is the reply whose `job` call (`wait` or `collect`) consumed the result, at `consume_tool_job`, the one point every consumption passes.
   A background child whose result is never consumed is counted on no reply; its record still holds its usage.
3. A child turn's cost is its own priced usage (`run_cost_usd` over `turn_usage(prior_usage_of(response), response.metrics)` with `model_prices(config)`) plus the subagent cost its own turn received.
   It is computed once when the child settles (`settle_child_response`) and kept on `DelegationChild`, so a job consumed after a restart still carries it.
   Nested children therefore roll up: a reviewer's own subagents count toward the reviewer's cost, and the reviewer's toward the reply.
4. The received cost is collected in a per-turn holder bound in a context variable for every agent turn, top-level and delegated.
   The handover points add to it; the metadata builder reads it when it builds, not when the builder partial is made, because the tool calls run after that.
   Children of unknown cost (no priced model) add nothing.
5. Budgets keep using `cost.usd` (`reply_cost_usd`).
   The usage-ledger scan already counts child spend; adding `subagents_usd` to the per-reply fast path could count a child twice when the scan has already covered it.

## Rejected

- Folding child usage into the reply's `usage` and `usd`: it mixes other models' tokens into the reply's own usage and context numbers.
- Counting a background child on the reply that launched it: that reply is sent before the child ends, so it would need editing an old event.
- Per-subagent cost on the `io.mindroom.subagents` frames for the subagents panel: useful, but a separate change; the reply and thread totals answer "what did this cost" first.

## Delivery

1. Backend (`mindroom`): price settled children, collect received cost per turn, publish `cost.subagents_usd`.
2. Client (`mindroom-chat`): parse `cost.subagents_usd`, show it as its own row in the reply's cost details, and add it to the thread total.
   Without the backend change the client shows what it shows today.

## Testing

- Backend: a scripted parent turn that runs a foreground child on a priced model publishes `subagents_usd` equal to the child's priced usage; a background child counts on the reply whose `job collect` consumed it; a nested child rolls up into its parent child's cost.
- Client: parser unit test for `subagents_usd`; thread-total test that sums both fields; reply details shows the row.
