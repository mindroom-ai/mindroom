"""Replay of memory-recall arms through MindRoom's indexer and hybrid search, and the benchmark CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from evals.memory_recall import judge, run
from evals.memory_recall.arms import ARMS, replay
from evals.memory_recall.judge import Verdict
from evals.memory_recall.run import arm_metrics
from evals.memory_recall.turns import Turn, write_turns
from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from tests.knowledge_test_support import install_knowledge_embedder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.memory._shared import MemoryResult

FIXTURE_MEMORY = Path(__file__).parent / "fixtures" / "memory_recall" / "memory"
CONFIG_YAML = """\
agents: {}
models:
  default:
    provider: openai
    id: test-model
memory:
  embedder:
    provider: openai
    config:
      model: test-embedder
      dimensions: 1
"""
METRIC_KEYS = {
    "turns",
    "mean_tokens",
    "p95_tokens",
    "relevant_per_1k_tokens",
    "turns_with_relevant",
    "unrelated_only_turns",
    "abstained_turns",
    "judge_errors",
}
THREAD = "!room:example.org:$thread-root"
ROOM = "!room:example.org"


class _CountingEmbedder:
    """Embeds every text to the same one-dimension vector, so hybrid search ranks by its keyword half."""

    sends_batches = False

    def __init__(self) -> None:
        self.document_texts = 0

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        if purpose != "query":
            self.document_texts += len(texts)
        return [[1.0] for _text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> _CountingEmbedder:
    """Index and search with a counting fake instead of the configured embedder."""
    fake = _CountingEmbedder()
    install_knowledge_embedder(monkeypatch, lambda *_args, **_kwargs: fake)
    return fake


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """A copy of the fixture corpus outside any Git repository, so undated files stay undated."""
    return Path(shutil.copytree(FIXTURE_MEMORY, tmp_path / "workspace" / "memory"))


def _turn(day: str, query: str, session_id: str = THREAD) -> Turn:
    return Turn(
        ts=datetime.fromisoformat(f"{day}T12:00:00-08:00"),
        agent="alpha",
        session_id=session_id,
        query=query,
        injected_hits=(),
        injected_tokens=0,
    )


def _write_config(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML)
    return path


def _load(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=_write_config(tmp_path),
        storage_path=tmp_path / "out" / "index",
        process_env={},
    )
    return load_config(runtime_paths), runtime_paths


async def _replay(tmp_path: Path, memory_dir: Path, arm: str, turns: list[Turn]) -> list[list[MemoryResult]]:
    config, runtime_paths = _load(tmp_path)
    return await replay(
        turns,
        ARMS[arm],
        memory_dir=memory_dir,
        config=config,
        runtime_paths=runtime_paths,
        index_dir=tmp_path / "out" / "index",
    )


def _sources(results: list[MemoryResult]) -> list[str]:
    return [(result.get("metadata") or {})["source_file"] for result in results]


def _judge_transport() -> httpx.MockTransport:
    """Rate the first hit of each request directly relevant and the rest unrelated."""

    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        count = prompt.count("=== HIT ")
        hits = [{"relevance": 2 if index == 0 else 0, "why": "test"} for index in range(count)]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"hits": hits})}}]})

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_run_smoke_on_fixture(
    tmp_path: Path,
    memory_dir: Path,
    embedder: _CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI writes every arm's metrics without memory text, and A1 never returns an excluded file."""
    assert embedder is not None
    monkeypatch.setattr(judge, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-test")
    turns = [
        _turn("2026-01-15", "What colour is the garden shed and what is kept inside it?"),
        _turn("2026-01-25", "And the roof?"),
        _turn("2026-01-25", "Which invoice arrived by email?", session_id=ROOM),
    ]
    write_turns(turns, tmp_path / "turns.jsonl")
    out = tmp_path / "out"

    await run.run_cli(
        [
            "--turns",
            str(tmp_path / "turns.jsonl"),
            "--memory-dir",
            str(memory_dir),
            "--config",
            str(_write_config(tmp_path)),
            "--arms",
            "A0,A1,A2,A3,A4",
            "--out",
            str(out),
            "--key-env",
            "TEST_JUDGE_KEY",
        ],
        transport=_judge_transport(),
    )

    results = json.loads((out / "results.json").read_text())
    assert set(results) == {"A0", "A1", "A2", "A3", "A4"}
    for metrics in results.values():
        assert set(metrics) == METRIC_KEYS
        assert metrics["turns"] == 3
        assert metrics["judge_errors"] == 0
    report = (out / "results.md").read_text()
    assert all(arm in report for arm in results)
    for written in (json.dumps(results), report):
        assert "garden shed" not in written
        assert "invoice" not in written

    a0_sources = {source for hits in await _replay(tmp_path, memory_dir, "A0", turns) for source in _sources(hits)}
    a1_sources = {source for hits in await _replay(tmp_path, memory_dir, "A1", turns) for source in _sources(hits)}
    assert "memory/agent_email/inbox.md" in a0_sources
    assert a1_sources
    assert not any(
        source.startswith(("memory/archive/", "memory/agent_email/")) or source == "memory/2026-01-05-0930.md"
        for source in a1_sources
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["A0", "A2"])
async def test_replay_drops_hits_dated_after_turn(
    tmp_path: Path,
    memory_dir: Path,
    embedder: _CountingEmbedder,
    arm: str,
) -> None:
    """A note written after the turn is not recalled, whether it is a whole chunk (A0) or one entry (A2)."""
    assert embedder is not None
    query = "copper roof sheets"
    before, after = await _replay(
        tmp_path,
        memory_dir,
        arm,
        [_turn("2026-01-15", query), _turn("2026-01-25", query)],
    )
    assert not any("2026-01-20" in source for source in _sources(before))
    assert "copper" in after[0]["memory"]
    assert "2026-01-20" in _sources(after)[0]


@pytest.mark.asyncio
async def test_chunk_is_dated_by_the_entries_it_holds(
    tmp_path: Path,
    memory_dir: Path,
    embedder: _CountingEmbedder,
) -> None:
    """A file spanning several 5,000-character chunks dates each chunk by its own entries, not the whole file's."""
    assert embedder is not None
    old = [
        f"- [id=m_20260101{n:06d}_old{n}] The lighthouse keeper logged visit number {n} in the ledger."
        for n in range(65)
    ]
    new = [
        f"- [id=m_20260301{n:06d}_new{n}] The submarine crew logged dive number {n} in the ledger." for n in range(20)
    ]
    (memory_dir / "ledger.md").write_text("\n".join([*old, *new]) + "\n")
    assert len("\n".join(old)) > 5000

    [hits] = await _replay(tmp_path, memory_dir, "A0", [_turn("2026-02-01", "lighthouse keeper visit ledger")])
    ledger = [hit["memory"] for hit in hits if _sources([hit]) == ["memory/ledger.md"]]
    assert len(ledger) == 1
    assert "lighthouse" in ledger[0]
    assert "submarine" not in ledger[0]


@pytest.mark.asyncio
async def test_entry_arm_recalls_single_entries(tmp_path: Path, memory_dir: Path, embedder: _CountingEmbedder) -> None:
    """With entry chunks, a hit is one note entry, not the whole daily file."""
    assert embedder is not None
    [hits] = await _replay(tmp_path, memory_dir, "A2", [_turn("2026-01-15", "salmon kibble for Pixel")])
    assert hits[0]["memory"].startswith("- [id=m_20260110100000_bbbb2222]")
    assert "garden shed" not in hits[0]["memory"]
    assert _sources(hits)[0] == "memory/2026-01-10.md.entries/2026-01-10__2.md"


@pytest.mark.asyncio
async def test_short_follow_up_in_thread_injects_nothing(
    tmp_path: Path,
    memory_dir: Path,
    embedder: _CountingEmbedder,
) -> None:
    """A4 skips recall for a short reply in a thread, but not for a short room message or a long reply."""
    assert embedder is not None
    turns = [
        _turn("2026-01-25", "And the roof?"),
        _turn("2026-01-25", "And the roof?", session_id=ROOM),
        _turn("2026-01-25", "What was the garden shed roof replaced with, and when?"),
    ]
    a3 = await _replay(tmp_path, memory_dir, "A3", turns)
    a4 = await _replay(tmp_path, memory_dir, "A4", turns)
    assert all(a3)
    assert a4[0] == []
    assert a4[1] == a3[1]
    assert a4[2] == a3[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["A0", "A2"])
async def test_undated_file_is_dated_by_first_commit(
    tmp_path: Path,
    memory_dir: Path,
    embedder: _CountingEmbedder,
    arm: str,
) -> None:
    """An undated file counts from its first commit; outside Git it is kept rather than dropped."""
    assert embedder is not None
    turns = [_turn("2026-01-25", "lawn mower and bicycle pump")]
    [kept] = await _replay(tmp_path / "plain", memory_dir, arm, turns)
    assert any(source.startswith("memory/garden.md") for source in _sources(kept))

    _commit_all(memory_dir.parent, day="2026-01-30")
    [dropped] = await _replay(tmp_path / "git", memory_dir, arm, turns)
    assert not any(source.startswith("memory/garden.md") for source in _sources(dropped))


def _commit_all(workspace: Path, *, day: str) -> None:
    """Commit every file of ``workspace`` in a new Git repository, dated ``day``."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
        "GIT_AUTHOR_DATE": f"{day}T12:00:00",
        "GIT_COMMITTER_DATE": f"{day}T12:00:00",
    }
    for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "notes"]):
        subprocess.run(["git", "-C", str(workspace), *command], check=True, env=env)


@pytest.mark.asyncio
async def test_rerun_reuses_cached_index(tmp_path: Path, memory_dir: Path, embedder: _CountingEmbedder) -> None:
    """A second replay of the same corpus embeds no documents; a changed corpus builds a new index."""
    turns = [_turn("2026-01-25", "garden shed")]
    first = await _replay(tmp_path, memory_dir, "A2", turns)
    embedded = embedder.document_texts
    assert embedded > 0
    assert await _replay(tmp_path, memory_dir, "A2", turns) == first
    assert embedder.document_texts == embedded
    (memory_dir / "2026-01-21.md").write_text("- [id=m_20260121090000_eeee5555] The shed got a new lock.\n")
    await _replay(tmp_path, memory_dir, "A2", turns)
    assert embedder.document_texts > embedded


def test_metrics_exclude_judge_errors() -> None:
    """Errored verdicts count only as judge errors; abstentions, relevance and tokens come from judged hits."""
    verdicts: list[list[Verdict | None]] = [
        [Verdict(2, ""), Verdict(0, "")],
        [Verdict(0, "")],
        [],
        [None],
    ]
    metrics = arm_metrics(verdicts, tokens=[100, 50, 0, 40], hit_tokens=[[45, 45], [45], [], [35]])
    assert metrics["turns"] == 4
    assert metrics["judge_errors"] == 1
    assert metrics["abstained_turns"] == 0.25
    # The turn whose only hit errored has nothing judged, so the shares are over the other three turns.
    assert metrics["turns_with_relevant"] == pytest.approx(1 / 3)
    assert metrics["unrelated_only_turns"] == pytest.approx(1 / 3)
    # One relevant hit over the 150 tokens of turns with judged hits plus the 5 template tokens of the errored turn.
    assert metrics["relevant_per_1k_tokens"] == pytest.approx(1 / 0.155)
    assert metrics["mean_tokens"] == pytest.approx(47.5)
    assert metrics["p95_tokens"] == 100
