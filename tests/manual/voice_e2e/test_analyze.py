# ruff: noqa: INP001
"""Offline checks for the voice-call harness analysis helpers."""

from datetime import UTC, datetime

from analyze import Burst, DelegateTurn, Utterance, delegation_answered, delegation_timing

S = 1_000_000_000


def _stage(delegation_id: str, stage: str, t: float) -> dict:
    return {
        "event": "call_delegation_stage",
        "delegation_id": delegation_id,
        "stage": stage,
        "timestamp": datetime.fromtimestamp(t, UTC).isoformat(),
    }


def test_answer_audio_is_attributed_only_before_the_next_utterance() -> None:
    """A delegate result followed only by audio after the next utterance has no answer timing."""
    utterance = Utterance("question", first_voiced_ns=0, last_voiced_ns=1 * S)
    turn = DelegateTurn(request_s=2.0, prep_s=2.5, llm_s=[8.0])
    goodbye = Burst(40 * S, 41 * S)

    timing = delegation_timing(utterance, [turn], [goodbye], next_start_ns=3 * S)
    assert timing is not None
    assert timing["result_to_audio_ms"] is None
    assert timing["speech_end_to_answer_audio_ms"] is None

    timing = delegation_timing(utterance, [turn], [Burst(9 * S, 10 * S), goodbye], next_start_ns=float("inf"))
    assert timing is not None
    assert timing["result_to_audio_ms"] == 1000


def test_delegation_is_answered_after_every_delegation_finished_and_the_agent_spoke() -> None:
    """An acknowledgement before the result does not finish a delegated step."""
    created = [_stage("a", "created", 10.0)]
    assert not delegation_answered([], last_voiced_s=11.0)
    assert not delegation_answered(created, last_voiced_s=11.0)

    done = [*created, _stage("a", "response_done", 20.0)]
    assert not delegation_answered(done, last_voiced_s=15.0)
    assert delegation_answered(done, last_voiced_s=21.0)
    assert not delegation_answered([*done, _stage("b", "created", 20.5)], last_voiced_s=21.0)

    failed = {"event": "call_live_delegation_failed", "delegation_id": "a", "timestamp": done[1]["timestamp"]}
    assert delegation_answered([*created, failed], last_voiced_s=21.0)
