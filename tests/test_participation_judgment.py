"""Judgment backends decide participation at the real gate, falling back to the reply model's own check."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.tools import MEDIA_FOLLOW_UP_TEXT
from mindroom.attachments.storage import AttachmentRecord, format_attachment_annotation, format_attachments_prompt
from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.history.interrupted_replay import render_stopped_attempt
from mindroom.history.summary_message import compaction_summary_message
from mindroom.hooks.enrichment import render_transient_context
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.llm.media import File, Image
from mindroom.llm.messages import MEDIA_MSGTYPE_KEY, Message
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from mindroom.participation import ParticipationGate
from mindroom.participation_hook import participation_hook
from mindroom.participation_judgment import create_participation_decider
from mindroom.tool_system.events import ToolTraceEntry
from tests.agent_loop.helpers import collect, context, spec
from tests.conftest import install_model_providers, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.runs import RunRecord
    from tests.fakes.scripted_model import ScriptStep

_RESPOND = '{"action":"respond","reason":"Useful help."}'


async def _fail_judge() -> None:
    msg = "Provider failed"
    raise RuntimeError(msg)


async def _cancel_judge() -> None:
    raise asyncio.CancelledError


def _response(probability: object = 0.9, *, model: str = _PINNED_MODEL) -> bytes:
    return json.dumps(
        {
            "model": model,
            "answers": {"participation": {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 100, "output_tokens": 1},
        },
    ).encode()


def _gate(
    tmp_path: Path,
    *,
    threshold: float = 0.8,
    key: str = "test-secret",
    timeout: float = 1.5,
    backend: str = "typesafe",
) -> ParticipationGate:
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": key})
    config = Config(
        agents={"helper": {"display_name": "Helper"}},
        models={"cheap": {"provider": "openai", "id": "cheap"}},
    )
    persist_entity_accounts(config, paths)
    participation = ParticipationConfig.model_validate(
        {
            "instructions": "Offer technical help.",
            "judgment": {"provider": "typesafe", "threshold": threshold, "timeout_seconds": timeout}
            if backend == "typesafe"
            else {"provider": "llm", "model": "cheap", "timeout_seconds": timeout},
        },
    )
    return ParticipationGate(
        instructions=participation.instructions,
        decider=create_participation_decider(
            participation,
            config,
            paths,
            agent_name="helper",
        ),
    )


async def _reply(
    model: ScriptedModel,
    gate: ParticipationGate,
    messages: Sequence[Message],
    *,
    run_id: str = "run-1",
) -> RunRecord:
    """Run one reply under the gate and return its record."""
    _, record = await collect(
        run_agent(
            spec(model),
            list(messages),
            ctx=context(run_id=run_id),
            hooks=LoopHooks(before_request=(participation_hook(gate, model),)),
            persist=None,
            request_template=EMPTY_REQUEST,
        ),
    )
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "approved"), [(0.8, True), (0.79, False)])
async def test_typesafe_controls_real_gate_without_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float,
    approved: bool,
) -> None:
    """Threshold mistakes or a redundant in-model check change the delivered answer."""
    posted: list[dict[str, Any]] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        assert _self._api_key == "test-secret"
        posted.append(json.loads(body))
        return _response(score)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    model = ScriptedModel([answer("Useful answer"), answer("Useful answer")])
    gate = _gate(tmp_path, key=" \ttest-secret\n")
    messages = [
        Message(role="system", content="Private workspace instructions"),
        Message(
            role="user",
            content=render_transient_context(["Private memory and hook context"]),
            add_to_agent_memory=False,
        ),
        Message(role="user", content="Can anyone explain this?"),
    ]

    for run_id in ("run-1", "run-2"):
        record = await _reply(model, gate, messages, run_id=run_id)
        assert (record.content or "") == ("Useful answer" if approved else "")

    assert gate.approved is approved
    assert len(posted) == 1
    assert posted[0]["questions"]["participation"]["type"] == "noul"
    assert "Offer technical help." in json.dumps(posted[0])
    assert "Private workspace" not in json.dumps(posted[0])
    assert "Private memory" not in json.dumps(posted[0])
    assert len(model.requests) == (2 if approved else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_key",
        "timeout",
        "transport",
        "invalid",
        "drift",
        "redacted",
        "oversized",
        "unicode",
    ],
)
async def test_typesafe_failure_uses_existing_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Provider failures and incomplete text must retain the current participation path."""
    posts: list[bytes] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        posts.append(body)
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "transport":
            message = "unavailable"
            raise httpx.ConnectError(message)
        if failure == "invalid":
            return _response(True)
        if failure == "drift":
            return _response(model="unexpected-version")
        return _response(0.0)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path, key="" if failure == "missing_key" else "test-secret", timeout=0.01)
    content = {
        "redacted": "api_key=secret-value",
        "oversized": "x" * 20_000,
        "unicode": "\ud800",
    }.get(failure, "Any ideas?")
    messages = [Message(role="user", content=content)]
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])

    record = await _reply(model, gate, messages)

    assert record.content == "Useful answer"
    assert gate.approved
    assert len(model.requests) == 2
    assert len(posts) == (1 if failure in {"timeout", "transport", "invalid", "drift"} else 0)


@pytest.mark.asyncio
async def test_typesafe_cancellation_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled turn must not start another provider call or acquire approval."""

    async def post(_self: JudgmentClient, _body: bytes) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Should never be called")])
    with pytest.raises(asyncio.CancelledError):
        await _reply(model, gate, [Message(role="user", content="Any ideas?")])
    assert gate.decision is None
    assert model.requests == []


@pytest.mark.asyncio
async def test_typesafe_drops_prompt_metadata_and_preserves_speaker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending raw Matrix metadata would expose stable IDs and display names unnecessarily."""
    posted: list[dict[str, Any]] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        posted.append(json.loads(body))
        return _response(0.0)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Should not run")])
    messages = [
        Message(
            role="user",
            content='<msg event_id="$a" from="@alice:example.org" display_name="Alice"><![CDATA[Question?]]></msg>',
        ),
        Message(role="user", content='<msg event_id="$b" from="@bob:example.org"><![CDATA[Answer.]]></msg>'),
        Message(role="user", content='<msg event_id="$c" from="@alice:example.org"><![CDATA[Thanks.]]></msg>'),
    ]

    await _reply(model, gate, messages)

    wire = json.dumps(posted[0])
    assert not any(value in wire for value in ("@alice", "@bob", "Alice", "$a", "$b", "$c"))
    texts = [message["text"] for message in posted[0]["state"]["conversation"]]
    assert texts == [
        '<msg from="speaker_1"><![CDATA[Question?]]></msg>',
        '<msg from="speaker_2"><![CDATA[Answer.]]></msg>',
        '<msg from="speaker_1"><![CDATA[Thanks.]]></msg>',
    ]
    assert model.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "requests"), [(0.95, 1), (0.05, 0), (None, 1)])
async def test_typesafe_decides_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float | None,
    requests: int,
) -> None:
    """An approved reply reaches the provider with its tools selectable, and a refused one never does.

    An abstention falls back to the model's own decision, which an ordinary answer fails.
    """

    async def post(_self: JudgmentClient, _body: bytes) -> bytes:
        return _response(score)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Useful answer")])

    record = await _reply(model, gate, [Message(role="user", content="Any ideas?")])

    assert (record.content or "") == ("Useful answer" if score == 0.95 else "")
    assert gate.approved is (score == 0.95)
    assert len(model.requests) == requests
    if score == 0.95:
        assert model.requests[0].tool_choice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_dedicated_llm_uses_same_gate_without_calling_reply_model_for_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
) -> None:
    """Changing the judge backend must preserve visible behavior and isolate the reply model."""
    judge = ScriptedModel([answer(json.dumps({"decision": approved}))])
    selected: list[str] = []

    def load(_config: Config, _paths: object, model_name: str) -> ScriptedModel:
        selected.append(model_name)
        return judge

    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", load)
    reply = ScriptedModel([answer("Useful answer")])
    gate = _gate(tmp_path, backend="llm", key="")
    messages = [Message(role="system", content="Private system prompt"), Message(role="user", content="Any ideas?")]

    record = await _reply(reply, gate, messages)

    assert (record.content or "") == ("Useful answer" if approved else "")
    assert gate.approved is approved
    assert selected == ["cheap"]
    assert len(judge.requests) == 1
    assert len(reply.requests) == int(approved)
    request = judge.requests[0]
    assert not request.tools
    assert request.tool_choice == "none"
    assert "Private system prompt" not in str(request.messages)
    assert "Offer technical help." in str(request.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn",
    [
        answer('{"decision": null}'),
        answer('{"decision": "yes"}'),
        calls(tool_call("unsafe", "call"), content='{"decision": true}'),
        [Await(_fail_judge)],
    ],
)
async def test_dedicated_llm_abstention_uses_existing_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn: list[ScriptStep],
) -> None:
    """Both explicit abstention and judge failures preserve the existing fallback policy."""
    judge = ScriptedModel([turn])
    install_model_providers(monkeypatch, {"cheap": judge})
    reply = ScriptedModel([answer(_RESPOND), answer("Useful answer")])
    gate = _gate(tmp_path, backend="llm")

    record = await _reply(reply, gate, [Message(role="user", content="Any ideas?")])

    assert record.content == "Useful answer"
    assert gate.approved
    assert len(judge.requests) == 1
    assert len(reply.requests) == 2


@pytest.mark.asyncio
async def test_dedicated_llm_cancellation_does_not_start_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation has the same unsettled outcome for either backend."""
    judge = ScriptedModel([[Await(_cancel_judge)]])
    install_model_providers(monkeypatch, {"cheap": judge})
    reply = ScriptedModel([answer("Must not run")])
    gate = _gate(tmp_path, backend="llm")
    with pytest.raises(asyncio.CancelledError):
        await _reply(reply, gate, [Message(role="user", content="Any ideas?")])
    assert gate.decision is None
    assert reply.requests == []


_AGENTS = {"helper": {"display_name": "Helper"}, "scout": {"display_name": "Scout"}}


def _decisions_body(probability: float) -> bytes:
    return json.dumps(
        {
            "model": OPENAI_DECISIONS_MODEL,
            "answers": [{"type": "predicate", "name": "participation", "probability": probability}],
            "usage": {"input_tokens": 100, "output_tokens": 1},
        },
    ).encode()


def _decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probability: float,
) -> tuple[ParticipationGate, dict[str, str], list[dict[str, Any]]]:
    """Return a gate judged by OpenAI Decisions, the agents' Matrix IDs, and the request bodies it posts."""
    paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(paths).save_credentials("openai", {"api_key": "test-openai-key"})
    config = Config(agents=_AGENTS, models={"default": {"provider": "openai", "id": "reply"}})
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, paths).items()}
    posted: list[dict[str, Any]] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        posted.append(json.loads(body))
        return _decisions_body(probability)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    participation = ParticipationConfig.model_validate(
        {"instructions": "Offer technical help.", "judgment": {"provider": "openai_decisions"}},
    )
    decider = create_participation_decider(participation, config, paths, agent_name="helper")
    return ParticipationGate(instructions=participation.instructions, decider=decider), ids, posted


def _msg(sender: str, body: str, *, event_id: str = "$e") -> str:
    return f'<msg event_id="{event_id}" from="{sender}" display_name="Private Name"><![CDATA[{body}]]></msg>'


def _record(attachment_id: str, kind: str, filename: str) -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id,
        Path(f"/private/{filename}"),
        kind,  # type: ignore[arg-type]
        filename=filename,
        sender="@alice:example.org",
        source_event_id="$file",
    )


def _busy_thread(ids: dict[str, str]) -> list[Message]:
    """A shared thread where the agent used tools, another agent spoke, and people sent files."""
    image = format_attachment_annotation([_record("att_image", "image", "salary-review.png")])
    current = format_attachments_prompt([_record("att_file", "file", "divorce-contract.pdf")])
    return [
        Message(role="user", content=_msg("@alice:example.org", "Can someone check the nightly build?")),
        Message(
            role="assistant",
            content="Checking.",
            tool_calls=[tool_call("shell", "call-1", command="cat /etc/SECRET_ARGUMENT")],
        ),
        Message(role="tool", content="SECRET_TOOL_RESULT", tool_call_id="call-1", tool_name="shell"),
        Message(role="assistant", content="The build passes. Send a screenshot if it fails again."),
        Message(role="user", content=_msg(ids["scout"], "I saw the same failure yesterday.")),
        Message(role="user", content=_msg("@bob:example.org", f"Here is mine.\n{image}")),
        Message(
            role="assistant",
            content="",
            tool_calls=[tool_call("web_search", "call-2", query="SECRET_QUERY")],
            images=[Image(content=b"SECRET_IMAGE_BYTES")],
        ),
        Message(role="tool", content="SECRET_SEARCH_RESULT", tool_call_id="call-2", tool_name="web_search"),
        Message(
            role="user",
            content=f"Current message:\n{_msg('@alice:example.org', f'Thanks, Bob, that is the one.\n\n{current}')}",
            files=[File(content=b"SECRET_FILE_CONTENT", filename="divorce-contract.pdf")],
        ),
    ]


@pytest.mark.asyncio
async def test_decisions_judge_reads_tool_calls_and_files_as_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy thread reaches the decisions judge with tool names and file kinds, never arguments, results or files."""
    gate, ids, posted = _decisions(tmp_path, monkeypatch, 0.05)
    model = ScriptedModel([answer("Must not run")])

    with capture_logs() as logs:
        record = await _reply(model, gate, _busy_thread(ids))

    assert (record.content or "") == ""
    assert gate.is_declined
    assert model.requests == []
    assert len(posted) == 1
    wire = json.dumps(posted[0])
    for private in (
        "SECRET",
        "salary-review",
        "divorce-contract",
        "att_image",
        "att_file",
        "/private",
        "@alice",
        "@bob",
        ids["scout"],
        "Private Name",
        "$e",
    ):
        assert private not in wire
    assert [message["content"] for message in posted[0]["input"]] == [
        'user: <msg from="speaker_1"><![CDATA[Can someone check the nightly build?]]></msg>',
        "assistant: Checking.\n[used tools: shell]\nThe build passes. Send a screenshot if it fails again.",
        'user: <msg from="agent_1"><![CDATA[I saw the same failure yesterday.]]></msg>',
        'user: <msg from="speaker_2"><![CDATA[Here is mine.\n[sent an image]]]></msg>',
        "assistant: [sent an image]\n[used tools: web_search]",
        'user: Current message:\n<msg from="speaker_1"><![CDATA[Thanks, Bob, that is the one.\n\n[sent a file]]]></msg>',
    ]
    assert "Helper" in posted[0]["questions"][0]["instructions"]
    assert [entry["judge"] for entry in logs if entry["event"] == "Participation judged"] == ["decisions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "body", "placeholder"),
    [
        ("user", "{name}", "[sent a file]"),
        ("user", "Here it is.\n{name}", "[sent a file]"),
        ("assistant", "\n\n{name}", "[sent a file]"),
        ("assistant", "\n\n[audio message: {name}]", "[sent audio]"),
    ],
)
async def test_an_uncaptioned_file_reaches_the_judge_without_its_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    body: str,
    placeholder: str,
) -> None:
    """A file sent without a caption has its name as its body, which must not reach the judge either."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.9)
    name = 'tax "return" 2026.pdf'
    kind = "audio" if "audio" in placeholder else "file"
    text = f"{body.format(name=name)}\n{format_attachment_annotation([_record('att_file', kind, name)])}"
    messages = [
        Message(role="user", content=_msg("@alice:example.org", "Who has the latest copy?")),
        Message(role=role, content=_msg("@bob:example.org", text) if role == "user" else text),
        Message(role="user", content=_msg("@alice:example.org", "Thanks!")),
    ]
    model = ScriptedModel([answer("Useful answer")])

    await _reply(model, gate, messages)

    wire = json.dumps(posted[0])
    assert "tax" not in wire
    assert "return" not in wire
    assert placeholder in posted[0]["input"][1]["content"]
    assert ("Here it is." in wire) is ("Here it is." in body)


@pytest.mark.asyncio
async def test_a_media_message_reaches_the_judge_as_its_kind_whatever_its_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A media event with no attachment record keeps its file name as body; the judge reads only its kind."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.9)
    messages = [
        Message(
            role="user",
            content=_msg("@bob:example.org", "tax-return-2026.pdf"),
            provider_data={MEDIA_MSGTYPE_KEY: "m.file"},
        ),
        Message(
            role="assistant",
            content="\n\n[audio message: voice-note.ogg]",
            provider_data={MEDIA_MSGTYPE_KEY: "m.audio"},
        ),
        Message(role="user", content=_msg("@alice:example.org", "Got it, thanks.")),
    ]
    model = ScriptedModel([answer("Useful answer")])

    await _reply(model, gate, messages)

    assert [message["content"] for message in posted[0]["input"]] == [
        'user: <msg from="speaker_1"><![CDATA[[sent a file]]]></msg>',
        "assistant: [sent audio]",
        'user: <msg from="speaker_2"><![CDATA[Got it, thanks.]]></msg>',
    ]


@pytest.mark.asyncio
async def test_a_long_tool_loop_does_not_push_people_out_of_the_judge_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One reply's tool calls reach the judge as one agent message, so the request it answers stays in view."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.9)
    loop: list[Message] = []
    for index in range(7):
        loop.append(Message(role="assistant", content="", tool_calls=[tool_call("shell", f"call-{index}")]))
        loop.append(Message(role="tool", content="SECRET_RESULT", tool_call_id=f"call-{index}", tool_name="shell"))
    loop.append(Message(role="user", content=MEDIA_FOLLOW_UP_TEXT, images=[Image(content=b"SECRET_IMAGE")]))
    messages = [
        Message(role="user", content=_msg("@alice:example.org", "Helper, audit prod and staging.")),
        *loop,
        Message(role="assistant", content="Prod is fine."),
        Message(role="user", content=_msg("@bob:example.org", "What about the other one?")),
    ]
    model = ScriptedModel([answer("Useful answer")])

    await _reply(model, gate, messages)

    assert [message["content"] for message in posted[0]["input"]] == [
        'user: <msg from="speaker_1"><![CDATA[Helper, audit prod and staging.]]></msg>',
        "assistant: " + "\n".join(["[used tools: shell]"] * 7) + "\nProd is fine.",
        'user: <msg from="speaker_2"><![CDATA[What about the other one?]]></msg>',
    ]


@pytest.mark.asyncio
async def test_interrupted_tool_context_and_summaries_reach_the_judge_without_tool_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted reply quotes its tool calls and a summary retells them; the judge reads only the tool names."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.9)
    tool = ToolTraceEntry(
        "tool_call_completed",
        "read_file",
        args_preview='{"path": "SECRET_PATH"}',
        result_preview="SECRET_RESULT",
    )
    stopped = render_stopped_attempt(partial_text="Reading it now.", completed_tools=[tool], interrupted_tools=[])
    messages = [
        compaction_summary_message("Earlier the agent read SECRET_SUMMARY.", run_local=False),
        Message(role="assistant", content=stopped),
        Message(role="user", content=_msg("@alice:example.org", "Did that finish?")),
    ]
    model = ScriptedModel([answer("Useful answer")])

    record = await _reply(model, gate, messages)

    assert record.content == "Useful answer"
    assert "SECRET" not in json.dumps(posted[0])
    assert [message["content"] for message in posted[0]["input"]] == [
        "assistant: Reading it now.\n\n(turn stopped before completion; 1 tool call(s) had finished)\n\n[used tools: read_file]",
        'user: <msg from="speaker_1"><![CDATA[Did that finish?]]></msg>',
    ]


@pytest.mark.asyncio
async def test_people_talking_after_a_tool_reply_reach_the_judge_as_people(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge sees who speaks, that the agent used tools, and the newest message in full, so it can stay silent."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.1)
    newest = "Lunch at noon? " + "We can talk about the trip. " * 100
    messages = [
        Message(role="user", content=_msg("@alice:example.org", "Helper, what's the weather in Paris?")),
        Message(role="assistant", content="", tool_calls=[tool_call("weather", "call-1", city="Paris")]),
        Message(role="tool", content="18C", tool_call_id="call-1", tool_name="weather"),
        Message(role="assistant", content="It is 18C and sunny in Paris. " * 200),
        Message(role="user", content=_msg("@bob:example.org", "Nice, see you there.")),
        Message(role="user", content=f"Current message:\n{_msg('@alice:example.org', newest)}"),
    ]
    model = ScriptedModel([answer("Must not run")])

    await _reply(model, gate, messages)

    assert gate.is_declined
    assert model.requests == []
    contents = [message["content"] for message in posted[0]["input"]]
    assert contents[0] == 'user: <msg from="speaker_1"><![CDATA[Helper, what\'s the weather in Paris?]]></msg>'
    # The agent's own long reply is shortened to its start and end, and the newest message arrives whole.
    assert contents[1].startswith("assistant: [used tools: weather]\nIt is 18C")
    assert "characters omitted" in contents[1]
    assert contents[2:] == [
        'user: <msg from="speaker_2"><![CDATA[Nice, see you there.]]></msg>',
        f'user: Current message:\n<msg from="speaker_1"><![CDATA[{newest}]]></msg>',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["text", "tool_name", "sender"])
async def test_credential_like_content_falls_back_to_the_reply_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Anything credential-like, including a placeholder's tool name or a sender, keeps the reply model's judgment."""
    gate, _ids, posted = _decisions(tmp_path, monkeypatch, 0.9)
    secret = "sk-proj-" + "a1B2c3D4e5" * 4
    sender = f"@{secret}:example.org" if where == "sender" else "@alice:example.org"
    messages = [
        Message(
            role="assistant",
            content="Done.",
            tool_calls=[tool_call(secret if where == "tool_name" else "shell", "call-1")],
        ),
        Message(role="user", content=_msg(sender, f"Try {secret}" if where == "text" else "Any ideas?")),
    ]
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])

    with capture_logs() as logs:
        record = await _reply(model, gate, messages)

    assert record.content == "Useful answer"
    assert posted == []
    assert len(model.requests) == 2
    judged = [entry for entry in logs if entry["event"] == "Participation judged"]
    assert [(entry["judge"], entry["fallback"]) for entry in judged] == [("reply_model", "essential_input_redacted")]


@pytest.mark.asyncio
async def test_without_a_judgment_backend_the_reply_model_judges(tmp_path: Path) -> None:
    """No backend keeps the reply model's decision and logs it."""
    del tmp_path
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])

    with capture_logs() as logs:
        record = await _reply(model, ParticipationGate(), [Message(role="user", content="Any ideas?")])

    assert record.content == "Useful answer"
    judged = [entry for entry in logs if entry["event"] == "Participation judged"]
    assert [(entry["judge"], entry["fallback"]) for entry in judged] == [("reply_model", "no_judgment_backend")]
