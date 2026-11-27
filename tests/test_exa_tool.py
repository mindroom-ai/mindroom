"""Tests for the native Exa toolkit transport timeout and Agent Runs research API."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from exa_py import ExaTimeoutError
from exa_py.agent.types import AgentError, AgentGroundingCitation, AgentGroundingEntry, AgentOutput, AgentRun

from mindroom.tools import exa as exa_module

if TYPE_CHECKING:
    import pytest


class _FakeAgentRuns:
    def __init__(self, created_run: AgentRun, terminal_run: AgentRun) -> None:
        self._created_run = created_run
        self._terminal_run = terminal_run
        self.create_calls: list[dict[str, Any]] = []
        self.poll_calls: list[tuple[str, int | None]] = []

    def create(self, **kwargs: Any) -> AgentRun:  # noqa: ANN401
        self.create_calls.append(kwargs)
        return self._created_run

    def poll_until_finished(self, run_id: str, *, timeout_ms: int | None = None) -> AgentRun:
        self.poll_calls.append((run_id, timeout_ms))
        return self._terminal_run


class _FakeExaClient:
    def __init__(self, api_key: str | None, *, timeout: float | None = None, runs: _FakeAgentRuns) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.agent = SimpleNamespace(runs=runs)


def test_exa_configures_sdk_timeout_and_runs_research_via_agent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exa configures the SDK transport timeout directly and runs research via agent.runs.create + poll_until_finished."""
    created_run = AgentRun(id="run_123", status="queued")
    terminal_run = AgentRun(
        id="run_123",
        status="completed",
        output=AgentOutput(
            structured={"summary": "Matrix is an open protocol."},
            grounding=[
                AgentGroundingEntry(
                    field="summary",
                    citations=[
                        AgentGroundingCitation(url="https://spec.matrix.org", title="Matrix Spec"),
                    ],
                ),
            ],
        ),
    )
    fake_runs = _FakeAgentRuns(created_run=created_run, terminal_run=terminal_run)
    captured_clients: list[_FakeExaClient] = []

    def fake_exa_cls(api_key: str | None, *, timeout: float | None = None) -> _FakeExaClient:
        client = _FakeExaClient(api_key, timeout=timeout, runs=fake_runs)
        captured_clients.append(client)
        return client

    monkeypatch.setattr(
        exa_module,
        "_require_exa",
        lambda: exa_module._ExaImports(exa_cls=fake_exa_cls, timeout_error_cls=ExaTimeoutError),
    )

    tool = exa_module.exa_tools()(
        api_key="exa-secret",
        timeout=45,
        enable_research=True,
        research_model="exa-research-pro",
    )
    output_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    raw_result = tool.research("Summarize Matrix federation", output_schema=output_schema)
    parsed = json.loads(raw_result)

    assert len(captured_clients) == 1
    assert captured_clients[0].api_key == "exa-secret"
    assert captured_clients[0].timeout == 45
    assert fake_runs.create_calls == [
        {
            "query": "Summarize Matrix federation",
            "effort": "high",
            "output_schema": output_schema,
        },
    ]
    assert fake_runs.poll_calls == [("run_123", None)]
    assert parsed == {
        "id": "run_123",
        "status": "completed",
        "data": {"summary": "Matrix is an open protocol."},
        "citations": {
            "summary": [{"url": "https://spec.matrix.org", "title": "Matrix Spec"}],
        },
    }


def test_exa_research_handles_text_output_failed_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unstructured text output, failed runs, and SDK timeouts serialize cleanly."""
    text_runs = _FakeAgentRuns(
        created_run=AgentRun(id="run_text", status="queued"),
        terminal_run=AgentRun(
            id="run_text",
            status="completed",
            output=AgentOutput(text="Plain research summary"),
        ),
    )
    monkeypatch.setattr(
        exa_module,
        "_require_exa",
        lambda: exa_module._ExaImports(
            exa_cls=lambda api_key, *, timeout=None: _FakeExaClient(api_key, timeout=timeout, runs=text_runs),
            timeout_error_cls=ExaTimeoutError,
        ),
    )

    tool = exa_module.exa_tools()(api_key="exa-secret", enable_research=True)
    text_result = json.loads(tool.research("Explain Exa"))
    assert text_runs.create_calls == [{"query": "Explain Exa", "effort": "auto"}]
    assert text_result == {
        "id": "run_text",
        "status": "completed",
        "data": "Plain research summary",
        "citations": {},
    }

    failed_runs = _FakeAgentRuns(
        created_run=AgentRun(id="run_fail", status="queued"),
        terminal_run=AgentRun(
            id="run_fail",
            status="failed",
            error=AgentError(message="Upstream budget exceeded"),
        ),
    )
    tool.exa = _FakeExaClient("exa-secret", timeout=30, runs=failed_runs)
    assert json.loads(tool.research("Explain Exa")) == {"error": "Upstream budget exceeded"}

    class _TimeoutRuns(_FakeAgentRuns):
        def poll_until_finished(self, run_id: str, *, timeout_ms: int | None = None) -> AgentRun:
            del run_id, timeout_ms
            msg = "timed out"
            raise ExaTimeoutError(msg)

    tool.exa = _FakeExaClient(
        "exa-secret",
        timeout=30,
        runs=_TimeoutRuns(
            created_run=AgentRun(id="run_to", status="queued"),
            terminal_run=AgentRun(id="run_to", status="completed"),
        ),
    )
    assert json.loads(tool.research("Explain Exa")) == {"error": "Research task timed out"}


def test_exa_lookup_actions_option_whitelists_and_toggle_narrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """exa_lookup dispatches search/contents/similar/answer, preserves SDK option whitelists, and narrows schema."""
    from mindroom.tool_system.toolkits import adapt_toolkit  # noqa: PLC0415

    calls: list[tuple[str, dict[str, Any]]] = []

    class _RichExaClient(_FakeExaClient):
        def __init__(self, api_key: str | None, *, timeout: float | None = None) -> None:
            super().__init__(api_key, timeout=timeout, runs=SimpleNamespace())  # type: ignore[arg-type]

        def search_and_contents(self, query: str, **kwargs: Any) -> Any:  # noqa: ANN401
            calls.append(("search", {"query": query, **kwargs}))
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        url="https://example.com",
                        title="Example",
                        author="Alice",
                        published_date="2026-01-01",
                        text="abcdefghij",
                        summary="Short",
                        highlights=["hi"],
                    ),
                ],
            )

        def get_contents(self, *, urls: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
            calls.append(("contents", {"urls": urls, **kwargs}))
            return SimpleNamespace(results=[])

        def find_similar_and_contents(self, *, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
            calls.append(("similar", {"url": url, **kwargs}))
            return SimpleNamespace(results=[])

        def answer(self, *, query: str, **kwargs: Any) -> Any:  # noqa: ANN401
            calls.append(("answer", {"query": query, **kwargs}))
            return SimpleNamespace(
                answer="42",
                citations=[
                    SimpleNamespace(
                        id="c1",
                        url="https://example.com",
                        title="T",
                        published_date=None,
                        author=None,
                        text="full body",
                    ),
                ],
            )

    monkeypatch.setattr(
        exa_module,
        "_require_exa",
        lambda: exa_module._ExaImports(exa_cls=_RichExaClient, timeout_error_cls=ExaTimeoutError),
    )

    tool = exa_module.exa_tools()(api_key="k", text_length_limit=4, summary=True, include_domains=["example.com"])
    assert list(tool.functions) == ["exa_lookup"]

    search_out = json.loads(tool.exa_lookup("search", query="matrix", category="news"))
    assert search_out == [
        {
            "url": "https://example.com",
            "title": "Example",
            "author": "Alice",
            "published_date": "2026-01-01",
            "text": "abcd",
            "summary": "Short",
            "highlights": ["hi"],
        },
    ]
    assert json.loads(tool.exa_lookup("contents", urls=["https://example.com"])) == []
    assert json.loads(tool.exa_lookup("similar", url="https://example.com", num_results=3)) == []
    ans = json.loads(tool.exa_lookup("answer", query="why?", text=True))
    assert ans["answer"] == "42"
    assert ans["citations"][0]["text"] == "full body"
    assert "include_domains" in calls[0][1]
    assert "include_domains" not in calls[1][1]
    assert "include_domains" in calls[2][1]
    assert calls[2][1]["num_results"] == 3

    only_contents = exa_module.exa_tools()(
        api_key="k",
        enable_search=False,
        enable_find_similar=False,
        enable_answer=False,
    )
    offered = adapt_toolkit(only_contents).functions[0]
    assert offered.parameters["properties"]["action"]["enum"] == ["contents"]
    assert set(offered.parameters["properties"]) == {"action", "urls"}
    assert "`search`" not in (offered.description or "")
    assert "linkedin profile" not in (offered.description or "")
    assert "disabled" in only_contents.exa_lookup("search", query="x").lower()

    none_enabled = exa_module.exa_tools()(
        api_key="k",
        enable_search=False,
        enable_get_contents=False,
        enable_find_similar=False,
        enable_answer=False,
    )
    assert list(none_enabled.functions) == []
