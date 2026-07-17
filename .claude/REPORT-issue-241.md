# ISSUE-241 investigation report

## Incident timeline correction

The reported Matrix thread was not interrupted by the 09:51 systemd restart.
The systemd journal shows PID 1034630 stopping at 09:51:48 and the replacement PID 1974470 starting at 09:51:59.
Startup stale-stream recovery ran from 09:52:36 through 09:53:56 in PID 1974470.
The affected placeholder event `$dAw5iTGPvSZAFGxe4Jd6piivYXojd0sA9D1BxW9C-EA` was created later, at 13:16:56, according to `mindroom_data/event_cache.db`.
The same process, PID 1974470, was still deferring a config reload at 13:20:39, forced it at 13:21:08, and cancelled the openclaw bot sync task at 13:21:29.
At 13:21:34 the journal recorded `Streaming response interrupted by sync restart` for thread `$3Puh1tAoPXxMrBrXtYjsQ0RHQhh32i_43bkguPPaN1k`.
At 13:21:35 the old openclaw bot logged `sync_restart_retry_queued`, but no later `sync_restart_retry_started` exists.
The config replacement completed at 13:22:14 with a newly constructed bot and therefore a newly constructed retry queue.

This timing matters because no startup scan could have collected an event that did not exist until more than three hours after the scan.

## Q1: Who wrote the restart-interrupted edit?

The dying openclaw bot wrote the edit during the 13:21 graceful entity replacement, not startup cleanup in a new process.
`src/mindroom/streaming.py:285-301` maps a `sync_restart` cancellation to `RESTART_INTERRUPTED_RESPONSE_NOTE` and `STREAM_STATUS_ERROR`.
`src/mindroom/streaming.py:645-696` applies that terminal update from `StreamingResponse.finalize()`.
`src/mindroom/streaming.py:1835-1889` catches the response cancellation, drains pending delivery, calls `finalize(cancel_source=cancel_source)`, and re-raises a delivery error.
The cached latest edit `$ob8K_y16WxhKIagooG19NdKr5xm3y84JtYb6ubDchTo` has timestamp 13:21:34, contains `**[Response interrupted by service restart]**`, and has `io.mindroom.stream_status: error`.
Startup cleanup reads and classifies Matrix events in `src/mindroom/matrix/stale_stream_cleanup.py`; it does not write this terminal interrupted edit.

## Q2: What predicate controls resume eligibility?

`src/mindroom/matrix/stale_stream_cleanup.py:156-163` enables collection when `defaults.auto_resume_after_restart` is true.
`src/mindroom/matrix/stale_stream_cleanup.py:1692-1700` classifies the restart note as resume-eligible when the rendered body contains `RESTART_INTERRUPTED_RESPONSE_NOTE` and `stream_status` is absent, `error`, or `interrupted`.
`src/mindroom/matrix/stale_stream_cleanup.py:1703-1723` additionally requires a thread identifier and rejects an event that already has an auto-resume relay.
`src/mindroom/matrix/stale_stream_cleanup.py:534-586` applies the startup cutoff and age policy before collecting the candidate.

The affected event does not fail this predicate.
Its latest state contains the restart note, `stream_status: error`, and the expected thread relation.
The graceful `sync_restart` path therefore writes exactly one of the states the scanner accepts.

## Q3: Why did the other two threads qualify?

The three terminal states have the same relevant classification fields.

| Original target | Latest edit | Terminal status | Body marker |
| --- | --- | --- | --- |
| `$LAJBTDJQUpfwTHyQQixn0oWtukK1z9NT-xEdtyQBPro` | `$5BHV…` | `error` | restart-interrupted note |
| `$5HModZecPWs_8WYIMBDGLcpTUjkrQJEnhkEGQqgRErQ` | `$O93…` | `error` | restart-interrupted note |
| `$dAw5iTGPvSZAFGxe4Jd6piivYXojd0sA9D1BxW9C-EA` | `$ob8K…` | `error` | restart-interrupted note |

The first two targets existed before the 09:52 startup scan and were collected.
`src/mindroom/matrix/stale_stream_cleanup.py:375-414` then correctly skipped both because newer human activity existed.
The affected target was created at 13:16:56 and could not appear in the earlier scan.
The only 09:53 access to its eventual thread identifier was an unrelated thread-prewarm cache read; no placeholder for the later turn existed then.

## Q4: Does mid-tool cancellation use a different terminal path?

Mid-tool and mid-text streaming use the same terminal finalization path.
Tool progress and text chunks are consumed by the same response stream in `src/mindroom/streaming.py:1703-1833`.
Any `CancelledError` enters the common handler in `src/mindroom/streaming.py:1835-1889` and calls `StreamingResponse.finalize()` with the classified cancellation source.
There is no tool-specific branch that changes the terminal Matrix status or omits the restart note.

## Root cause

The actual interruption was a forced hot-config replacement, not a process restart.
`src/mindroom/bot.py:325` constructs a separate `SyncRestartRetryQueue` for every bot instance.
`src/mindroom/turn_controller.py:1600-1635` registers an in-memory retry closure after the interrupted response finalizes.
`src/mindroom/bot.py:1214-1232` only flushes that queue after the same bot receives a later healthy sync response.
The config lifecycle replaces the interrupted bot instance, so the old queue and its callback become unreachable before they can flush.
`src/mindroom/sync_restart_retry.py` is intentionally an in-process sync-watchdog mechanism and provides no cross-instance handoff.

The `restart-resume` plugin is unrelated because it only scans explicitly tagged idle threads on `bot:ready`.

## Fix direction

Changing the terminal status writer or broadening startup collection would not address this incident because the writer and predicate already agree.
The fix should preserve or reconstruct the existing interrupted-turn handoff across a bot replacement, using the existing Matrix terminal marker rather than adding another persistence mechanism.
The newer-human-activity predicate must remain unchanged.
