"""Cached LLM relevance judge for the memory recall benchmark."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from evals.memory_recall import judge
from evals.memory_recall.judge import JudgeCache, Verdict, cache_key, judge_hits

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MODEL = "test/judge-model"
QUERY = "What colour is the garden shed?"
HITS = ("The garden shed is painted green.", "The cat is called Pixel.")


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge, "_RETRY_DELAY_S", 0.0)


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _verdicts(*relevances: int) -> str:
    return json.dumps({"hits": [{"relevance": r, "why": f"r{r}"} for r in relevances]})


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), requests


async def _judge(
    hits: tuple[str, ...],
    cache: JudgeCache,
    client: httpx.AsyncClient,
    query: str = QUERY,
) -> list[Verdict | None]:
    return await judge_hits(query, hits, cache=cache, client=client, model=MODEL, api_key="sk-test")


@pytest.mark.asyncio
async def test_parses_relevance_0_1_2(tmp_path: Path) -> None:
    """One request carries every hit with the configured body, and each 0, 1 or 2 verdict is parsed in order."""
    client, requests = _client(lambda _request: _reply(_verdicts(2, 0, 1)))
    hits = (*HITS, "Tea is brewed at 80 degrees.")
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(hits, cache, client)

    assert verdicts == [Verdict(2, "r2"), Verdict(0, "r0"), Verdict(1, "r1")]
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1500
    prompt = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert QUERY in prompt
    assert "exactly 3 entries" in prompt
    assert "=== HIT 3 ===\nTea is brewed at 80 degrees." in prompt
    assert '"used"' not in prompt
    assert "REPLY" not in prompt


@pytest.mark.asyncio
async def test_cached_pair_skips_request(tmp_path: Path) -> None:
    """A query and hit judged once are read from the cache file next time, without a request."""
    path = tmp_path / "cache.sqlite"
    client, requests = _client(lambda _request: _reply(_verdicts(2, 0)))
    async with client:
        with JudgeCache(path) as cache:
            first = await _judge(HITS, cache, client)
        with JudgeCache(path) as cache:
            second = await _judge(HITS, cache, client)

    assert first == second == [Verdict(2, "r2"), Verdict(0, "r0")]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_only_uncached_hits_are_sent_and_merged_in_order(tmp_path: Path) -> None:
    """Only uncached hits go to the judge, and its verdicts merge with cached ones in the hits' order."""
    hits = (HITS[0], HITS[1], "Tea is brewed at 80 degrees.")
    client, requests = _client(lambda _request: _reply(_verdicts(2, 0)))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            cache.put(cache_key(MODEL, QUERY, HITS[1]), Verdict(1, "cached"))
            verdicts = await _judge(hits, cache, client)

    assert verdicts == [Verdict(2, "r2"), Verdict(1, "cached"), Verdict(0, "r0")]
    prompt = json.loads(requests[0].content)["messages"][0]["content"]
    assert "exactly 2 entries" in prompt
    assert HITS[1] not in prompt
    assert "=== HIT 2 ===\nTea is brewed at 80 degrees." in prompt


@pytest.mark.asyncio
async def test_wrong_count_retries_then_none(tmp_path: Path) -> None:
    """A reply with the wrong number of verdicts is retried; after 3 attempts the hits get None and nothing is cached."""
    client, requests = _client(lambda _request: _reply(_verdicts(2)))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(HITS, cache, client)
            assert all(cache.get(cache_key(MODEL, QUERY, hit)) is None for hit in HITS)

    assert verdicts == [None, None]
    assert len(requests) == 3


@pytest.mark.parametrize(
    "respond",
    [
        lambda: httpx.Response(500, text="upstream down"),
        lambda: httpx.Response(200, json={"error": {"message": "rate limited"}}),
        lambda: _reply("not json at all"),
        lambda: _reply(json.dumps({"hits": [{"relevance": 3, "why": "x"}, {"relevance": 0, "why": "y"}]})),
    ],
    ids=["http-error", "no-choices", "non-json", "relevance-out-of-range"],
)
@pytest.mark.asyncio
async def test_bad_response_retries_then_none(tmp_path: Path, respond: Callable[[], httpx.Response]) -> None:
    """HTTP errors, missing choices, non-JSON and out-of-range relevance are retried, then give None."""
    client, requests = _client(lambda _request: respond())
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(HITS, cache, client)

    assert verdicts == [None, None]
    assert len(requests) == 3


@pytest.mark.parametrize(
    "content",
    [
        "Sure! " + _verdicts(1, 2),
        _verdicts(1, 2) + " Hope that helps.",
        "Here you go:\n```json\n" + _verdicts(1, 2) + "\n```",
        "```json\n" + _verdicts(1, 2) + "\n```\n```json\n" + _verdicts(1, 2) + "\n```",
    ],
    ids=["prose-before", "prose-after", "prose-before-fence", "two-fences"],
)
@pytest.mark.asyncio
async def test_json_wrapped_in_prose_retries_then_none(tmp_path: Path, content: str) -> None:
    """A reply that is not one whole JSON document fails every attempt and caches nothing."""
    client, requests = _client(lambda _request: _reply(content))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(HITS, cache, client)
            assert all(cache.get(cache_key(MODEL, QUERY, hit)) is None for hit in HITS)

    assert verdicts == [None, None]
    assert len(requests) == 3


@pytest.mark.parametrize(
    "content",
    [
        "  \n" + _verdicts(1, 2) + "\n  ",
        "```json\n" + _verdicts(1, 2) + "\n```",
        "\n```\n" + _verdicts(1, 2) + "\n```\n",
    ],
    ids=["whitespace", "json-fence", "bare-fence"],
)
@pytest.mark.asyncio
async def test_accepts_whitespace_and_one_code_fence(tmp_path: Path, content: str) -> None:
    """Surrounding whitespace and a single code fence around the JSON are accepted."""
    client, requests = _client(lambda _request: _reply(content))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(HITS, cache, client)

    assert verdicts == [Verdict(1, "r1"), Verdict(2, "r2")]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_recovers_after_a_bad_attempt(tmp_path: Path) -> None:
    """A good reply after a bad one is used."""
    responses = iter([_reply("Sure! " + _verdicts(1, 2)), _reply(_verdicts(1, 2))])
    client, requests = _client(lambda _request: next(responses))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            verdicts = await _judge(HITS, cache, client)

    assert verdicts == [Verdict(1, "r1"), Verdict(2, "r2")]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_no_request_when_all_cached_or_empty(tmp_path: Path) -> None:
    """No hits, or only cached hits, need no request."""
    client, requests = _client(lambda _request: _reply(_verdicts()))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            cache.put(cache_key(MODEL, QUERY, HITS[0]), Verdict(2, "cached"))
            assert await _judge((), cache, client) == []
            assert await _judge((HITS[0],), cache, client) == [Verdict(2, "cached")]
    assert requests == []


@pytest.mark.asyncio
async def test_long_hit_and_query_are_clipped_head_and_tail(tmp_path: Path) -> None:
    """Hits over 6000 and queries over 5000 characters keep their head and tail in the prompt."""
    long_hit = "H" * 3000 + "m" * 10_000 + "T" * 3000
    long_query = "Q" * 2500 + "m" * 10_000 + "E" * 2500
    client, requests = _client(lambda _request: _reply(_verdicts(0)))
    async with client:
        with JudgeCache(tmp_path / "cache.sqlite") as cache:
            await _judge((long_hit,), cache, client, query=long_query)

    prompt = json.loads(requests[0].content)["messages"][0]["content"]
    assert "H" * 3000 + "\n[...]\n" + "T" * 3000 in prompt
    assert "Q" * 2500 + "\n[...]\n" + "E" * 2500 in prompt
    assert "mmmm" not in prompt


def test_cache_key_depends_on_every_part() -> None:
    """The cache key changes with the model, the query and the hit."""
    base = cache_key(MODEL, QUERY, HITS[0])
    assert base == cache_key(MODEL, QUERY, HITS[0])
    assert len({base, cache_key("other/model", QUERY, HITS[0]), cache_key(MODEL, "other", HITS[0])}) == 3
    assert cache_key(MODEL, QUERY, HITS[0]) != cache_key(MODEL, QUERY, HITS[1])
