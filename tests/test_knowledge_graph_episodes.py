"""Stored runs become graph episodes: speakers labeled, tool results kept as observations, secrets and noise removed."""

from __future__ import annotations

from datetime import UTC, datetime

from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput

from mindroom.config.knowledge_graph import KnowledgeGraphIngestConfig
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.knowledge_graph.episodes import classify_thread, render_episodes
from mindroom.prompt_message_tags import render_msg_tag

ROOM = "!room:example.test"
THREAD = "$thread"
SETTINGS = KnowledgeGraphIngestConfig()


def _run(
    run_id: str,
    *,
    created_at: int = 1_760_000_000,
    prompt: str | None = None,
    answer: str = "Atlas ships Friday.",
    tools: list[ToolExecution] | None = None,
    hook_source: str | None = None,
) -> RunOutput:
    metadata: dict[str, object] = {"room_id": ROOM, "thread_id": THREAD, "requester_id": "@alice:example.test"}
    if hook_source is not None:
        metadata[HOOK_SOURCE_RUN_METADATA_KEY] = hook_source
    user_text = (
        prompt if prompt is not None else render_msg_tag(sender="@alice:example.test", body="When does Atlas ship?")
    )
    return RunOutput(
        run_id=run_id,
        session_id=f"{ROOM}:{THREAD}",
        agent_id="mind",
        created_at=created_at,
        metadata=metadata,
        messages=[Message(role="user", content=user_text), Message(role="assistant", content=answer)],
        tools=tools or [],
    )


def _tool(name: str, result: str, **args: object) -> ToolExecution:
    return ToolExecution(tool_call_id=f"call-{name}", tool_name=name, tool_args=args, result=result)


def test_conversation_run_renders_speakers_and_a_separate_tool_episode() -> None:
    """People and the agent are labeled, and tool results become their own observation episode."""
    run = _run("r1", tools=[_tool("get_issue", "Issue 42: Atlas launch, owner @bob", repo="acme/atlas", number=42)])

    [chunk] = render_episodes([run], kind="conversation", agent_name="mind", settings=SETTINGS)

    conversation, tool_results = chunk.episodes
    assert conversation.kind == "conversation"
    assert conversation.body.splitlines() == [
        "person @alice:example.test: When does Atlas ship?",
        "agent mind: Atlas ships Friday.",
    ]
    assert conversation.source_description == f"conversation: room {ROOM} thread {THREAD}"
    assert tool_results.kind == "tool_results"
    assert 'get_issue({"number": 42, "repo": "acme/atlas"})' in tool_results.body
    assert "Issue 42: Atlas launch, owner @bob" in tool_results.body
    assert tool_results.source_description.startswith(f"tool_results: room {ROOM} thread {THREAD}; calls: get_issue ")
    assert conversation.reference_time == datetime.fromtimestamp(1_760_000_000, UTC)
    assert chunk.last_run == (1_760_000_000, "r1")


def test_several_people_in_one_prompt_are_each_labeled() -> None:
    """A coalesced prompt names each sender."""
    prompt = (
        "<messages>"
        + render_msg_tag(sender="@alice:x", body="Bob owns Atlas.")
        + render_msg_tag(
            sender="@bob:x",
            body="No, Carol does now.",
        )
        + "</messages>"
    )

    [chunk] = render_episodes([_run("r1", prompt=prompt)], kind="conversation", agent_name="mind", settings=SETTINGS)

    assert chunk.episodes[0].body.splitlines()[:2] == [
        "person @alice:x: Bob owns Atlas.",
        "person @bob:x: No, Carol does now.",
    ]


def test_exploration_thread_renders_only_tool_results() -> None:
    """The agent's own synthesis in an exploration thread is never evidence."""
    run = _run("r1", answer="I think Bob owns everything.", tools=[_tool("list_repos", "acme/atlas, acme/beacon")])

    [chunk] = render_episodes([run], kind="exploration", agent_name="mind", settings=SETTINGS)

    assert [episode.kind for episode in chunk.episodes] == ["tool_results"]
    assert "Bob owns everything" not in chunk.episodes[0].body


def test_automation_thread_renders_nothing() -> None:
    """Maintenance threads such as dreaming are not about the world."""
    assert render_episodes([_run("r1")], kind="automation", agent_name="mind", settings=SETTINGS) == []


def test_knowledge_graph_calls_are_never_rendered() -> None:
    """Graph lookups returned to the graph would read as fresh evidence."""
    run = _run("r1", tools=[_tool("search_graph", "Alice leads Atlas", query="Atlas")])

    [chunk] = render_episodes([run], kind="conversation", agent_name="mind", settings=SETTINGS)

    assert [episode.kind for episode in chunk.episodes] == ["conversation"]


def test_oversized_tool_result_is_clipped_before_redaction() -> None:
    """A huge result keeps its first part rather than failing redaction as a whole."""
    run = _run("r1", tools=[_tool("fetch_page", "Atlas launch notes. " + "x" * 200_000, url="https://example.test")])

    [chunk] = render_episodes([run], kind="conversation", agent_name="mind", settings=SETTINGS)

    body = chunk.episodes[1].body
    assert "Atlas launch notes." in body
    assert "[redaction failed]" not in body
    assert len(body) < SETTINGS.max_tool_result_chars + 500


def test_credentials_are_redacted() -> None:
    """Credential-like values never reach the graph."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    run = _run("r1", answer=f"Use token {token} for the API.", tools=[_tool("call_api", f"token={token}", token=token)])

    episodes = render_episodes([run], kind="conversation", agent_name="mind", settings=SETTINGS)[0].episodes

    assert all(token not in episode.body and token not in episode.source_description for episode in episodes)


def test_runs_merge_until_the_cap_and_split_only_at_run_boundaries() -> None:
    """Consecutive runs share an episode until the next would pass the cap."""
    settings = KnowledgeGraphIngestConfig(max_episode_chars=1000)
    runs = [_run(f"r{index}", created_at=1_760_000_000 + index, answer="a" * 400) for index in range(3)]

    chunks = render_episodes(runs, kind="conversation", agent_name="mind", settings=settings)

    assert [chunk.last_run for chunk in chunks] == [(1_760_000_001, "r1"), (1_760_000_002, "r2")]
    assert chunks[0].episodes[0].body.count("agent mind:") == 2
    assert all(len(chunk.episodes[0].body) <= 1000 for chunk in chunks)


def test_single_run_over_the_cap_is_clipped() -> None:
    """One run larger than the cap still produces an episode, clipped to it."""
    settings = KnowledgeGraphIngestConfig(max_episode_chars=1000)

    [chunk] = render_episodes(
        [_run("r1", answer="b" * 5000)],
        kind="conversation",
        agent_name="mind",
        settings=settings,
    )

    assert len(chunk.episodes[0].body) <= 1000


def test_classify_thread_uses_the_root_hook_source_then_automation_threads() -> None:
    """The root prompt's hook source decides; older automation threads are known by their recorded root."""
    assert classify_thread([_run("r1", hook_source="automation/exploration"), _run("r2")], set()) == "exploration"
    assert classify_thread([_run("r1", hook_source="automation/dreaming")], set()) == "automation"
    assert classify_thread([_run("r1")], {THREAD}) == "automation"
    assert classify_thread([_run("r1")], set()) == "conversation"
