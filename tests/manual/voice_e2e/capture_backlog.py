#!/usr/bin/env python3
"""Agent-side output backlog from MINDROOM_CALL_AUDIO_CAPTURE_DIR recordings.

backlog(t) = provider audio received by t - audio accepted by the native AudioSource by t.
Gated-out silence also counts as received, so only differences between two instants
inside one speech burst are meaningful; this prints them around stalls and barge-ins.

Usage:
    python tests/manual/voice_e2e/capture_backlog.py ~/.mindroom-voicetest/runs/<run>
"""

from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

SAMPLE_RATE = 24000


def cumulative(path: Path) -> tuple[list[int], list[int]]:
    """Return (t_wall_ns, cumulative samples) for one capture stream."""
    times, totals, total = [], [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        total += row["samples"]
        times.append(row["t_wall_ns"])
        totals.append(total)
    return times, totals


def at(series: tuple[list[int], list[int]], t_ns: int) -> int:
    """Cumulative samples recorded at or before `t_ns`."""
    times, totals = series
    index = bisect.bisect_right(times, t_ns) - 1
    return totals[index] if index >= 0 else 0


Series = tuple[list[int], list[int]]


def backlog_ms(provider: Series, published: Series, t_ns: int) -> float:
    """Received-minus-published audio at `t_ns`, in milliseconds."""
    return (at(provider, t_ns) - at(published, t_ns)) * 1000 / SAMPLE_RATE


def main() -> int:
    """Print backlog growth around stall markers and at barge-in onsets."""
    run = Path(sys.argv[1]).expanduser()
    captures = sorted((run / "agent_capture").glob("*/provider_out.jsonl"))
    if not captures:
        print("no agent capture in run")
        return 1
    capture = captures[-1].parent
    provider = cumulative(capture / "provider_out.jsonl")
    published = cumulative(capture / "published.jsonl")
    markers = [json.loads(line) for line in (run / "markers.jsonl").read_text(encoding="utf-8").splitlines()]
    mic = [json.loads(line) for line in (run / "mic.jsonl").read_text(encoding="utf-8").splitlines()]
    print(
        f"capture {capture.name}: provider {provider[1][-1] / SAMPLE_RATE:.1f}s, published {published[1][-1] / SAMPLE_RATE:.1f}s",
    )
    for marker in markers:
        if marker["step"] == "stall_injected":
            t = marker["t_ns"]
            base = backlog_ms(provider, published, t - 100_000_000)
            after = [round(backlog_ms(provider, published, t + int(s * 1e9)) - base) for s in (0.5, 1.0, 2.0, 3.0, 4.0)]
            print(f"stall {marker['seconds']}s: backlog growth at +0.5/1/2/3/4 s = {after} ms")
    for marker in markers:
        if marker["step"] == "say_start" and marker["label"].startswith("barge_in"):
            onset = next(row["t_ns"] for row in mic if row["t_ns"] >= marker["t_ns"] and row["voiced"])
            stream_start = provider[0][0]
            # backlog relative to the quietest point of the run, i.e. what the caller still had to hear
            floor = min(backlog_ms(provider, published, t) for t in range(stream_start, onset, 50_000_000))
            print(
                f"{marker['label']}: backlog at onset {round(backlog_ms(provider, published, onset) - floor)} ms above run minimum",
            )
            last_published = published[0][bisect.bisect_right(published[0], onset + 15_000_000_000) - 1]
            print(
                f"  provider deltas after onset until +5 s: {round((at(provider, onset + 5_000_000_000) - at(provider, onset)) * 1000 / SAMPLE_RATE)} ms of audio",
            )
            print(f"  last published frame {round((last_published - onset) / 1e6)} ms after onset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
