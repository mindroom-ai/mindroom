#!/usr/bin/env python3
"""Summarize one `voice_call_e2e.py` run directory.

All times are caller-side wall clock (same host as the agent), so they include
the full media path: caller mic -> SFU -> agent -> GPT-Live -> agent -> SFU -> caller.

Usage:
    python tests/manual/voice_e2e/analyze.py ~/.mindroom-voicetest/runs/<run>
"""

from __future__ import annotations

import itertools
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

VOICED_RMS = 200
MERGE_GAP_NS = 300_000_000
STOP_QUIET_NS = 700_000_000


@dataclass(frozen=True)
class Burst:
    """One contiguous voiced stretch of received agent audio."""

    start_ns: int
    end_ns: int

    @property
    def ms(self) -> float:
        """Duration in milliseconds."""
        return (self.end_ns - self.start_ns) / 1e6


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, tolerating a missing file."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rx_bursts(rx: list[dict]) -> list[Burst]:
    """Group voiced received frames into bursts separated by >300 ms of quiet."""
    bursts: list[Burst] = []
    start = end = None
    for row in rx:
        if row["rms"] <= VOICED_RMS:
            continue
        t = row["t_ns"]
        if start is None:
            start = end = t
        elif t - end > MERGE_GAP_NS:
            bursts.append(Burst(start, end))
            start = end = t
        else:
            end = t
    if start is not None:
        bursts.append(Burst(start, end))
    return bursts


def utterances(markers: list[dict], mic: list[dict]) -> list[dict]:
    """Return each spoken utterance with its first/last voiced mic frame."""
    out = []
    starts = [m for m in markers if m["step"] == "say_start"]
    for index, marker in enumerate(starts):
        until = starts[index + 1]["t_ns"] if index + 1 < len(starts) else float("inf")
        voiced = [row["t_ns"] for row in mic if row["voiced"] and marker["t_ns"] <= row["t_ns"] < until]
        if voiced:
            out.append({"label": marker["label"], "first_voiced_ns": voiced[0], "last_voiced_ns": voiced[-1]})
    return out


def reply_timing(utterance: dict, bursts: list[Burst], next_start_ns: float) -> dict:
    """Agent bursts after an utterance, relative to the end of the caller's speech."""
    end = utterance["last_voiced_ns"]
    after = [b for b in bursts if end < b.start_ns < next_start_ns]
    overlapping = [b for b in bursts if b.start_ns <= end < b.end_ns]
    return {
        "label": utterance["label"],
        "first_audio_ms": round((after[0].start_ns - end) / 1e6) if after else None,
        "burst_onsets_ms": [round((b.start_ns - end) / 1e6) for b in after],
        "burst_ms": [round(b.ms) for b in after],
        "agent_talking_at_end": bool(overlapping),
    }


def barge_in_timing(utterance: dict, rx: list[dict]) -> dict:
    """Time from the caller's interruption onset until the agent goes quiet for 700 ms."""
    onset = utterance["first_voiced_ns"]
    voiced_after = [row["t_ns"] for row in rx if row["t_ns"] >= onset and row["rms"] > VOICED_RMS]
    last = onset
    for t in voiced_after:
        if t - last > STOP_QUIET_NS:
            break
        last = t
    window = [t for t in voiced_after if t <= onset + 10_000_000_000]
    return {
        "label": utterance["label"],
        "stop_ms": round((last - onset) / 1e6),
        "agent_voiced_ms_in_10s_after_onset": len(window) * 10,
    }


def stall_impact(marker: dict, rx: list[dict], stats: list[dict]) -> dict:
    """Longest received quiet run around an injected stall, plus concealment deltas."""
    t0 = marker["t_ns"] - 200_000_000
    t1 = marker["t_ns"] + int((marker["seconds"] + 2.0) * 1e9)
    window = [row for row in rx if t0 <= row["t_ns"] <= t1]
    longest = run = 0
    for row in window:
        run = run + row["samples"] if row["rms"] <= VOICED_RMS else 0
        longest = max(longest, run)
    before = [s for s in stats if s["t_ns"] <= t0]
    after = [s for s in stats if s["t_ns"] >= t1]
    concealed = None
    if before and after:
        concealed = stats_summary([before[-1], after[0]]).get("concealment_events")
    return {
        "seconds": marker["seconds"],
        "longest_rx_quiet_ms": round(longest * 1000 / 24000),
        "voiced_ms_in_window": sum(row["samples"] for row in window if row["rms"] > VOICED_RMS) * 1000 // 24000,
        "concealment_events": concealed,
    }


def stats_summary(stats: list[dict]) -> dict:
    """Totals of the agent track's inbound-rtp counters over the call."""
    rows = []
    for sample in stats:
        for entry in sample["inbound"]:
            inbound = entry.get("inboundRtp", {})
            stream = inbound.get("inbound", {})
            received = inbound.get("received", {})
            rows.append({"t_ns": sample["t_ns"], **received, **stream})
    if not rows:
        return {}
    first, last = rows[0], rows[-1]

    def delta(key: str) -> float:
        return float(last.get(key, 0)) - float(first.get(key, 0))

    jitter = [float(row.get("jitter", 0)) for row in rows]
    emitted = delta("jitterBufferEmittedCount")
    return {
        "samples": len(rows),
        "packets_received": delta("packetsReceived"),
        "packets_lost": delta("packetsLost"),
        "concealed_samples": delta("concealedSamples"),
        "silent_concealed_samples": delta("silentConcealedSamples"),
        "concealment_events": delta("concealmentEvents"),
        "total_samples_received": delta("totalSamplesReceived"),
        "jitter_max_s": max(jitter),
        "jitter_buffer_delay_avg_ms": round(delta("jitterBufferDelay") / emitted * 1000, 1) if emitted else None,
    }


def rx_cadence(rx: list[dict]) -> dict:
    """Inter-arrival statistics of received frames at the caller."""
    gaps = sorted((b["t_ns"] - a["t_ns"]) / 1e6 for a, b in itertools.pairwise(rx))
    if not gaps:
        return {}
    return {
        "frames": len(rx),
        "p50_ms": round(gaps[len(gaps) // 2], 1),
        "p99_ms": round(gaps[int(len(gaps) * 0.99)], 1),
        "max_ms": round(gaps[-1], 1),
        "over_100ms": sum(1 for gap in gaps if gap > 100),
    }


def transcript_summary(path: Path) -> dict:
    """Count transcript lines per speaker and fragments starting with punctuation."""
    if not path.exists():
        return {}
    lines = re.findall(r"^- `[\d:]+` \*\*(\w+)\*\*: (.*)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    user = [text for speaker, text in lines if speaker == "user"]
    return {
        "user_lines": len(user),
        "assistant_lines": sum(1 for speaker, _ in lines if speaker != "user"),
        "user_lines_leading_punct": sum(1 for text in user if re.match(r"^[,.;:!?]", text.strip())),
    }


def delegations(run: Path) -> list[dict]:
    """Delegate turns from the instance log: request, preparation and final model output times."""
    rows = []
    for line in read_jsonl(run / "instance.jsonl"):
        event = line.get("event", "")
        if event in ("AI request", "Preparing agent and prompt", "LLM usage"):
            stamp = line["timestamp"].replace("Z", "+00:00")
            rows.append((datetime.fromisoformat(stamp).timestamp(), event, line))
    turns: list[dict] = []
    for t, event, line in rows:
        if event == "AI request":
            turns.append({"request_s": t, "prep_s": None, "llm_s": [], "output_tokens": 0})
        elif turns and event == "Preparing agent and prompt":
            turns[-1]["prep_s"] = t
        elif turns:
            turns[-1]["llm_s"].append(t)
            turns[-1]["output_tokens"] += int(line.get("output_tokens") or 0)
    return turns


def delegation_timing(utterance: dict, turns: list[dict], bursts: list[Burst], next_start_ns: float) -> dict | None:
    """Join one utterance to its delegate turn and the first agent audio after the result."""
    end_s = utterance["last_voiced_ns"] / 1e9
    turn = next((t for t in turns if end_s - 1 < t["request_s"] < next_start_ns / 1e9 and t["llm_s"]), None)
    if turn is None:
        return None
    done_s = turn["llm_s"][-1]
    answer = next((b for b in bursts if b.start_ns / 1e9 > done_s), None)
    return {
        "label": utterance["label"],
        "to_request_ms": round((turn["request_s"] - end_s) * 1000),
        "prep_ms": round((turn["prep_s"] - turn["request_s"]) * 1000) if turn["prep_s"] else None,
        "model_ms": round((done_s - (turn["prep_s"] or turn["request_s"])) * 1000),
        "model_calls": len(turn["llm_s"]),
        "output_tokens": turn["output_tokens"],
        "result_to_audio_ms": round((answer.start_ns / 1e9 - done_s) * 1000) if answer else None,
        "speech_end_to_answer_audio_ms": round((answer.start_ns / 1e9 - end_s) * 1000) if answer else None,
    }


def main() -> int:
    """Print and store the run summary."""
    run = Path(sys.argv[1]).expanduser()
    markers = read_jsonl(run / "markers.jsonl")
    mic = read_jsonl(run / "mic.jsonl")
    rx = read_jsonl(run / "rx.jsonl")
    bursts = rx_bursts(rx)
    spoken = utterances(markers, mic)
    replies, barge_ins, delegated = [], [], []
    turns = delegations(run)
    for index, utterance in enumerate(spoken):
        next_start = spoken[index + 1]["first_voiced_ns"] if index + 1 < len(spoken) else float("inf")
        if timing := delegation_timing(utterance, turns, bursts, next_start):
            delegated.append(timing)
        if utterance["label"].startswith("barge_in"):
            barge_ins.append(barge_in_timing(utterance, rx))
        replies.append(reply_timing(utterance, bursts, next_start))
    greeting_joined = next((m["t_ns"] for m in markers if m["step"] == "caller_joined_sfu"), None)
    summary = {
        "run": run.name,
        "greeting_first_audio_ms": round((bursts[0].start_ns - greeting_joined) / 1e6)
        if bursts and greeting_joined
        else None,
        "replies": replies,
        "barge_ins": barge_ins,
        "delegations": delegated,
        "rx_bursts": [asdict(b) | {"ms": round(b.ms)} for b in bursts],
        "webrtc_inbound": stats_summary(read_jsonl(run / "stats.jsonl")),
        "rx_cadence": rx_cadence(rx),
        "transcript": transcript_summary(run / "transcript.md"),
        "timeouts": [m for m in markers if m["step"].startswith("timeout")],
        "stalls": [
            stall_impact(m, rx, read_jsonl(run / "stats.jsonl")) for m in markers if m["step"] == "stall_injected"
        ],
        "silent_watch_bursts_ms": [
            round(b.ms)
            for start in [m["t_ns"] for m in markers if m["step"] == "silent_watch_start"]
            for end in [m["t_ns"] for m in markers if m["step"] == "silent_watch_end"]
            for b in bursts
            if start <= b.start_ns <= end
        ],
    }
    (run / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"run {run.name}")
    print(f"greeting first audio after join: {summary['greeting_first_audio_ms']} ms")
    for reply in replies:
        print(
            f"  {reply['label']:<16} first_audio={reply['first_audio_ms']} ms  onsets={reply['burst_onsets_ms']}"
            f"  bursts_ms={reply['burst_ms']}  talking_at_end={reply['agent_talking_at_end']}",
        )
    for item in delegated:
        print(f"  DELEGATED {item}")
    for item in barge_ins:
        print(
            f"  BARGE {item['label']}: stop={item['stop_ms']} ms  voiced_10s={item['agent_voiced_ms_in_10s_after_onset']} ms",
        )
    print(f"webrtc: {summary['webrtc_inbound']}")
    print(f"rx cadence: {summary['rx_cadence']}")
    print(f"transcript: {summary['transcript']}")
    for stall in summary["stalls"]:
        print(f"  STALL {stall}")
    if any(m["step"] == "silent_watch_start" for m in markers):
        print(f"  stale speech during silent watch: bursts_ms={summary['silent_watch_bursts_ms']}")
    if summary["timeouts"]:
        print(f"timeouts: {summary['timeouts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
