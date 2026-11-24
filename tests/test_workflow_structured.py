"""Structured agent output: the schema helpers (pure) and the turns that correct an answer (engine harness)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.delegation.background import delegation_child
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.agent_nodes import turn_job_id
from mindroom.workflow_runs.limits import run_usage
from mindroom.workflow_runs.models import WorkflowNodeState
from mindroom.workflow_runs.replay import node_digest
from mindroom.workflow_runs.structured import (
    SCHEMA_TURNS,
    SchemaError,
    check_schema,
    extract_json,
    schema_prompt,
    validation_error,
)
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.workflow_helpers import NodeFailed, WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_OK = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
_GOOD = 'It holds.\n```json\n{"ok": true}\n```'
_ASK = {"prompt": "Does it hold?", "schema": _OK}
_TEN_CENTS = usage(100_000, 0)


def test_invalid_schema_names_the_path_and_the_reason() -> None:
    """A schema that is no JSON Schema is refused with where it is wrong and why."""
    check_schema(_OK)
    with pytest.raises(SchemaError) as wrong:
        check_schema({"type": "object", "properties": {"ok": {"type": "bool"}}})

    assert str(wrong.value).startswith("schema is not valid JSON Schema: properties/ok/type: 'bool' is not valid")
    assert str(wrong.value).endswith(".")
    with pytest.raises(SchemaError) as whole:
        check_schema("object")
    assert str(whole.value).startswith("schema is not valid JSON Schema: schema: 'object' is not of type")


@pytest.mark.parametrize(
    ("reference", "why"),
    [
        ("#/$defs/missing", "does not resolve"),
        ("https://example.com/schema.json", "is not inside the schema"),
        ("file:///etc/passwd", "is not inside the schema"),
        ("other.json#/$defs/answer", "is not inside the schema"),
    ],
)
def test_schema_reaches_only_what_it_holds_itself(reference: str, why: str) -> None:
    """A reference is followed inside the schema alone: one that leaves it, or names nothing in it, is refused."""
    answer = {"$defs": {"answer": {"type": "boolean"}}}
    check_schema({**answer, "$ref": "#/$defs/answer"})
    # What a keyword takes as a value is no reference.
    check_schema({"const": {"$ref": "https://example.com/schema.json"}})
    for keyword in ("$ref", "$dynamicRef"):
        with pytest.raises(SchemaError) as refused:
            check_schema({**answer, "items": {keyword: reference}})
        assert str(refused.value) == f"schema is not valid JSON Schema: reference {reference!r} {why}."


@pytest.mark.parametrize(
    "schema",
    [{"$ref": "https://example.com/schema.json"}, {"$ref": "#/$defs/typo"}, {"$ref": "#"}],
)
def test_schema_that_cannot_be_applied_is_an_error_and_opens_no_connection(
    schema: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever a schema that got past its check does to the validator is a `SchemaError`, and nothing is fetched."""
    opened: list[object] = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **_options: opened.append(args))
    monkeypatch.setattr("socket.socket.connect", lambda *args: opened.append(args))

    with pytest.raises(SchemaError) as broken:
        validation_error({"ok": [True]}, schema)

    assert str(broken.value).startswith("schema cannot be applied: ")
    assert opened == []


def test_prompt_ends_with_the_instruction_and_the_schema() -> None:
    """The agent is told to end with one fenced json block, and is shown the schema."""
    said = schema_prompt("Does it hold?", {"type": "boolean"})

    assert said == (
        "Does it hold?\n\nEnd your answer with one fenced json block that matches this JSON Schema:\n"
        '{\n  "type": "boolean"\n}'
    )


def test_extract_takes_the_last_fenced_block() -> None:
    """The answer's value is its last fenced json block, else the whole answer; anything else is no value."""
    assert extract_json('First:\n```json\n{"ok": false}\n```\nNo, rather:\n```json\n{"ok": true}\n```\n') == {
        "ok": True,
    }
    assert extract_json(' {"ok": true} ') == {"ok": True}
    assert extract_json("```json\n[1, 2]\n```") == [1, 2]
    for text in ("no json here", "```json\n{broken\n```", ""):
        with pytest.raises(SchemaError) as none:
            extract_json(text)
        assert str(none.value) == "no JSON value found"


def test_validation_error_names_the_path() -> None:
    """The first error of a value says where it is, unless it is the whole value's."""
    nested = {"type": "object", "properties": {"problems": {"type": "array", "items": {"type": "string"}}}}

    assert validation_error({"ok": True}, _OK) is None
    assert validation_error({}, _OK) == "'ok' is a required property"
    assert validation_error({"problems": ["a", 3]}, nested) == "problems/1: 3 is not of type 'string'"


def test_null_is_a_valid_value_for_a_schema_that_allows_it() -> None:
    """JSON null is a value like any other."""
    assert extract_json("```json\nnull\n```") is None
    assert validation_error(None, {"type": ["null", "string"]}) is None
    assert validation_error(None, {"type": "string"}) == "None is not of type 'string'"


def test_schema_is_part_of_the_digest() -> None:
    """A call with another schema is another call: a resume does not reuse its saved answer."""
    assert node_digest("agent", _ASK) != node_digest("agent", {**_ASK, "schema": {"type": "object"}})
    assert node_digest("agent", _ASK) != node_digest("agent", {"prompt": "Does it hold?"})


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with one started run whose agents run as real jobs on a priced, scripted model."""
    options = {"extra_tools": ("workflow",), "require_approval": (), "pricing": ModelPricing(input=1, output=2)}
    with workflow_stack(tmp_path, other_agents=("helper",), delegate_to=("helper",), **options) as stack:
        monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        yield stack
        await stack.close()


def _turns(stack: WorkflowStack) -> list[str]:
    """Return the turn of each entry of the run's ledger for the call at `@0`, in order."""
    with closing(sqlite3.connect(stack.store.database_path)) as connection:
        rows = connection.execute(
            "SELECT CAST(turn AS TEXT) FROM workflow_usage WHERE run_id = ? AND key = '@0' ORDER BY rowid",
            (stack.run_id,),
        ).fetchall()
    return [turn for (turn,) in rows]


def _last_said(model: ScriptedModel) -> str:
    return str([message.content for message in model.requests[-1].messages if message.role == "user"][-1])


@pytest.mark.asyncio
async def test_invalid_schema_fails_before_any_agent_starts(stack: WorkflowStack) -> None:
    """A schema that is no JSON Schema fails its call with kind `schema`, and no agent runs for it."""
    model = stack.child_script()

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", {"prompt": "x", "schema": {"type": "bool"}})

    assert failed.value.kind == "schema"
    assert str(failed.value).startswith("schema is not valid JSON Schema: type: 'bool' is not valid")
    assert (model.requests, list(stack.jobs.retained_jobs())) == ([], [])


@pytest.mark.asyncio
async def test_second_turn_fixes_the_answer_in_the_same_session_with_its_own_job_and_usage(
    stack: WorkflowStack,
) -> None:
    """An answer that does not match is corrected by a further turn of the same agent: its own job and ledger entry."""
    model = stack.child_script(answer("no json here", _TEN_CENTS), answer(_GOOD, _TEN_CENTS))

    result = await stack.result("@0", "agent", _ASK, {"name": "checker"})

    run = stack.store.get_run(stack.run_id)
    jobs = {job.job_id: delegation_child(job) for job in stack.jobs.retained_jobs()}
    first, second = (jobs[turn_job_id(run, "@0", 1, turn)] for turn in (1, 2))
    node = stack.store.get_node(stack.run_id, "@0")
    assert result == {"ok": True}
    assert (first.session_id, first.subagent_id) == (second.session_id, second.subagent_id)
    assert (len(jobs), len(model.requests), _turns(stack)) == (2, 2, ["1", "2"])
    assert schema_prompt("Does it hold?", _OK) in str(model.requests[0].messages[-1].content)
    assert (
        "Your answer did not match the schema: no JSON value found. Reply with only the corrected fenced json block."
    ) in _last_said(model)
    assert (node.links["turn"], node.links["job_id"]) == (2, turn_job_id(run, "@0", 1, 2))
    assert run_usage(stack.store, stack.run_id)["cost_usd"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_correction_follows_a_turn_that_ran_a_subagent_of_its_own(stack: WorkflowStack) -> None:
    """What the subagent of a turn cost is an entry beside that turn's, and the correction is the turn after it."""
    delegated = tool_call("run_subagent", "c1", task="Help.", agent_name="helper")
    stack.child_script(calls(delegated, metrics=_TEN_CENTS), answer("no json here"), answer(_GOOD))
    stack.child_script(answer("helped", _TEN_CENTS), agent="helper")

    assert await stack.result("@0", "agent", _ASK) == {"ok": True}

    turns = _turns(stack)
    assert (turns[0], turns[1][:4], turns[2]) == ("1", "sub:", "2")
    assert stack.store.get_node(stack.run_id, "@0").links["made"] is True


@pytest.mark.asyncio
async def test_five_bad_answers_fail_with_the_last_error(stack: WorkflowStack) -> None:
    """After five turns the call fails with kind `schema` and what was wrong with the last answer."""
    bad = [answer("```json\n{}\n```") for _ in range(SCHEMA_TURNS - 1)]
    model = stack.child_script(*bad, answer('```json\n{"ok": "yes"}\n```'), answer(_GOOD))

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _ASK, {"name": "checker"})

    assert failed.value.kind == "schema"
    assert str(failed.value) == (
        "Agent 'checker' did not return JSON matching the schema after 5 attempts. "
        "Last error: ok: 'yes' is not of type 'boolean'."
    )
    assert (len(model.requests), _turns(stack)) == (5, ["1", "2", "3", "4", "5"])
    assert stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.FAILED


@pytest.mark.asyncio
async def test_agent_that_called_a_tool_and_never_met_the_schema_is_not_run_again_unasked(
    stack: WorkflowStack,
) -> None:
    """A turn that started a tool call may have written: its call ends indeterminate, as any other without a result."""
    wrote = calls(tool_call("append", "c1", line="a"))
    stack.child_script(wrote, *(answer("still no json") for _ in range(SCHEMA_TURNS)))

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _ASK)

    assert failed.value.kind == "indeterminate"
    assert "did not return JSON matching the schema after 5 attempts" in str(failed.value)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_null_answer_is_the_calls_result(stack: WorkflowStack) -> None:
    """An agent that answers null for a schema that allows it completes its call with None."""
    stack.child_script(answer("```json\nnull\n```"))

    result = await stack.result("@0", "agent", {"prompt": "Anything?", "schema": {"type": ["null", "string"]}})

    assert result is None
    assert stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.COMPLETED


@pytest.mark.asyncio
async def test_run_that_ends_while_an_answer_is_wrong_starts_no_correction(stack: WorkflowStack) -> None:
    """A Stop of the run reads the turn that ended and starts no further one."""
    model = stack.child_script(answer("no json here"), answer(_GOOD))
    original = agent_nodes.launch_child
    started: list[int] = []

    async def counted(*args: object, **options: object) -> object:
        started.append(int(options.get("turn", 1)))  # type: ignore[call-overload]
        if started[-1] > 1:
            # The run is stopped while the first answer is read: the correction that was about to start must not.
            await stack.engine.cancel(stack.run_id, reason="Stopped.")
        return await original(*args, **options)  # type: ignore[arg-type]

    agent_nodes.launch_child = counted  # type: ignore[assignment]
    try:
        await stack.submit("@0", "agent", _ASK)
        await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").state is not WorkflowNodeState.RUNNING)
    finally:
        agent_nodes.launch_child = original

    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_schema_that_cannot_be_applied_to_an_answer_ends_the_call_and_saves_its_cost(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call saved with a schema that cannot be applied ends on its first answer: nothing is left running."""
    monkeypatch.setattr(agent_nodes, "check_schema", lambda _schema: None)
    model = stack.child_script(answer(_GOOD, _TEN_CENTS), answer(_GOOD))

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", {"prompt": "x", "schema": {"$ref": "#/$defs/typo"}})

    assert failed.value.kind == "schema"
    assert str(failed.value).startswith("schema cannot be applied: ")
    # The call has its end first, and then the wait that read the turn gives the job back.
    await stack.until(lambda: not stack.jobs.awaited_job_ids())
    assert len(model.requests) == 1
    assert run_usage(stack.store, stack.run_id)["cost_usd"] == pytest.approx(0.1)


class _Forgetful:
    """A job runtime that no longer keeps any job, as after the retention of the first turn's."""

    def __init__(self, jobs: object) -> None:
        self._jobs = jobs

    def retained_jobs(self) -> tuple[()]:
        return ()

    def retained_job(self, _job_id: str) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._jobs, name)


@pytest.mark.asyncio
async def test_further_turn_whose_first_child_is_gone_is_told_the_task_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A further turn continues the first turn's child. Without it the agent starts anew, and needs the task too."""
    model = stack.child_script(answer("no json here"), answer(_GOOD))
    launch = agent_nodes.launch_child

    async def forgetful(engine: object, *args: object, **options: object) -> object:
        if options.get("turn", 1) > 1:  # type: ignore[operator]
            runtime = engine.agents()  # type: ignore[attr-defined]
            options["jobs"] = _Forgetful(runtime.jobs)
        return await launch(engine, *args, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_nodes, "launch_child", forgetful)

    assert await stack.result("@0", "agent", _ASK) == {"ok": True}

    said = _last_said(model)
    assert schema_prompt("Does it hold?", _OK) in said
    assert "Reply with only the corrected fenced json block." in said


async def _provider_fails() -> None:
    msg = "provider 500"
    raise RuntimeError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("ends", ["fails", "is cancelled", "times out", "never starts"])
async def test_correction_that_ends_without_an_answer_keeps_what_the_first_turn_did(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    ends: str,
) -> None:
    """No end of a further turn proves that the first turn's tool call did nothing: a resume does not repeat it."""
    gate = asyncio.Event()
    wrote, again = calls(tool_call("append", "c1", line="a")), calls(tool_call("append", "c2", line="b"))
    further = [Await(_provider_fails)] if ends == "fails" else [Await(gate.wait), *answer(_GOOD)]
    model = stack.child_script(wrote, answer("no json here"), further, again, answer(_GOOD))
    second = turn_job_id(stack.store.get_run(stack.run_id), "@0", 1, 2)
    start = stack.jobs.start

    async def stopped(job_id: str, **options: object) -> object:
        if job_id == second and ends == "never starts":
            # As a primary that stops between saving the further turn's handle and starting its job.
            msg = "the primary stopped"
            raise RuntimeError(msg)
        return await start(job_id, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(stack.jobs, "start", stopped)
    await stack.submit("@0", "agent", _ASK, {"timeout": 2.0} if ends == "times out" else {})
    await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").links.get("turn") == 2)
    if ends == "is cancelled":
        await stack.until(lambda: len(model.requests) == 3)
        await stack.jobs.request_cancel(second)
    # The wait for the node's agent has ended: with the node's end, or with the primary's.
    await stack.until(lambda: not stack.engine.live.tasks.get(stack.run_id))
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", _ASK)

    assert failed.value.kind == "indeterminate"
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_schema_and_output_path_are_refused_together(stack: WorkflowStack) -> None:
    """A file would hold an answer before it is checked, so a call that names both starts no agent and writes none."""
    model = stack.child_script(answer("not what the schema asks for"))

    with pytest.raises(NodeFailed) as failed:
        await stack.result("@0", "agent", {**_ASK, "output_path": "answer.txt"})

    assert (failed.value.kind, model.requests) == ("schema", [])
    assert str(failed.value) == "schema and output_path cannot be used together: return the value and write it."
    assert list(stack.tmp_path.rglob("answer.txt")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keyword", "reference"),
    [("$ref", "#/allOf/missing"), ("$dynamicRef", "#/allOf/missing"), ("$ref", "#/allOf/-")],
)
async def test_invalid_local_reference_fails_at_submit_without_queueing(
    stack: WorkflowStack,
    keyword: str,
    reference: str,
) -> None:
    """An invalid local pointer fails with kind `schema` before queueing, even while the run's slot is busy."""
    await stack.start(max_concurrent=1)
    gate = asyncio.Event()
    model = stack.child_script([Await(gate.wait), *answer(_GOOD)])
    first = await stack.submit("@0", "agent", _ASK)
    assert first.state == "running"

    bad = {"allOf": [{"type": "object"}], keyword: reference}
    receipt = await stack.submit("@1", "agent", {"prompt": "Check.", "schema": bad})
    node = stack.store.get_node(stack.run_id, "@1")

    expected = f"schema is not valid JSON Schema: reference {reference!r} does not resolve."
    assert receipt.state == "failed"
    assert receipt.error == {"kind": "schema", "message": expected}
    assert node is not None
    assert (node.state, node.handle) == (WorkflowNodeState.FAILED, None)

    gate.set()
    assert await stack.result("@0", "agent", _ASK) == {"ok": True}
    assert len(model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad", "good", "schema", "expected"),
    [
        ("NaN", "0", {"type": "number"}, 0),
        ("Infinity", "1.5", {"type": "number"}, 1.5),
        ("-Infinity", "-2", {"type": "number"}, -2),
        ('{"n": NaN}', '{"n": 3}', {"type": "object", "properties": {"n": {"type": "number"}}}, {"n": 3}),
    ],
)
async def test_non_finite_structured_answers_trigger_correction(
    stack: WorkflowStack,
    bad: str,
    good: str,
    schema: dict[str, object],
    expected: object,
) -> None:
    """Non-finite numbers (NaN, Infinity) are rejected before validation and trigger a schema correction."""
    model = stack.child_script(answer(bad), answer(good))

    result = await stack.result("@0", "agent", {"prompt": "Estimate.", "schema": schema})

    assert result == expected
    assert len(model.requests) == 2
    async with stack.http() as client:
        response = await client.get("nodes/n1")
    assert response.status_code == 200
    assert response.json()["result"] == expected


@pytest.mark.asyncio
async def test_genuine_json_null_stays_accepted_when_schema_allows_it(stack: WorkflowStack) -> None:
    """Rejecting non-finite numbers does not break genuine JSON null where the schema allows null."""
    stack.child_script(answer("null"))

    result = await stack.result("@0", "agent", {"prompt": "Estimate.", "schema": {"type": ["number", "null"]}})

    assert result is None


@pytest.mark.asyncio
async def test_fenced_json_with_embedded_code_fences_in_string(stack: WorkflowStack) -> None:
    """A closing Markdown fence is recognized only on its own line, so embedded fences inside JSON strings work."""
    obj = {"example": "```python\nprint(17)\n```"}
    message = "```json\n" + json.dumps(obj) + "\n```"
    model = stack.child_script(answer(message))

    result = await stack.result("@0", "agent", {"prompt": "Return a Markdown sample.", "schema": {"type": "object"}})

    assert result == obj
    assert len(model.requests) == 1


def test_check_schema_follows_resolved_targets_and_terminates_on_cycles() -> None:
    """Preflight follows internal $ref targets outside Draft 2020-12 keywords, with cycle detection."""
    # Valid recursive schema under OpenAPI-style components/schemas terminates and passes.
    recursive = {
        "$ref": "#/components/schemas/Node",
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "next": {"$ref": "#/components/schemas/Node"},
                        "$ref": {"type": "string"},
                    },
                    "const": {"$ref": "https://example.com/not-a-ref"},
                    "x-annotation": {"$ref": "#/components/schemas/Missing"},
                },
                "Unused": {"$ref": "#/components/schemas/AlsoMissing"},
            },
        },
    }
    check_schema(recursive)

    # Dangling internal ref inside a resolved components/schemas target is rejected.
    dangling = {
        "$ref": "#/components/schemas/Answer",
        "components": {"schemas": {"Answer": {"$ref": "#/components/schemas/Missing"}}},
    }
    with pytest.raises(SchemaError) as missing:
        check_schema(dangling)
    assert str(missing.value) == (
        "schema is not valid JSON Schema: reference '#/components/schemas/Missing' does not resolve."
    )

    # External ref inside a resolved components/schemas target is rejected.
    external = {
        "$ref": "#/components/schemas/Answer",
        "components": {"schemas": {"Answer": {"$ref": "https://example.com/external.json"}}},
    }
    with pytest.raises(SchemaError) as outside:
        check_schema(external)
    assert str(outside.value) == (
        "schema is not valid JSON Schema: reference 'https://example.com/external.json' is not inside the schema."
    )

    # Invalid JSON Schema target under components/schemas is rejected at preflight.
    invalid_target = {
        "$ref": "#/components/schemas/Answer",
        "components": {"schemas": {"Answer": {"type": "bool"}}},
    }
    with pytest.raises(SchemaError) as bad_target:
        check_schema(invalid_target)
    assert str(bad_target.value).startswith("schema is not valid JSON Schema: type: 'bool' is not valid")

    # Non-schema primitive target under components/schemas is rejected at preflight.
    primitive_target = {
        "$ref": "#/components/schemas/Answer",
        "components": {"schemas": {"Answer": "not-a-schema"}},
    }
    with pytest.raises(SchemaError) as bad_primitive:
        check_schema(primitive_target)
    assert str(bad_primitive.value).startswith("schema is not valid JSON Schema: schema: 'not-a-schema' is not of type")


@pytest.mark.asyncio
async def test_dangling_ref_inside_components_schema_fails_at_admission_with_zero_jobs_and_requests(
    stack: WorkflowStack,
) -> None:
    """An OpenAPI-style $ref whose target has a dangling $ref fails at admission before any job or model call."""
    model = stack.child_script(answer(_GOOD, _TEN_CENTS))
    bad = {
        "$ref": "#/components/schemas/Answer",
        "components": {"schemas": {"Answer": {"$ref": "#/components/schemas/Missing"}}},
    }

    receipt = await stack.submit("@0", "agent", {"prompt": "Check.", "schema": bad})
    node = stack.store.get_node(stack.run_id, "@0")

    expected = "schema is not valid JSON Schema: reference '#/components/schemas/Missing' does not resolve."
    assert receipt.state == "failed"
    assert receipt.error == {"kind": "schema", "message": expected}
    assert node is not None
    assert (node.state, node.handle) == (WorkflowNodeState.FAILED, None)
    assert stack.jobs.retained_jobs() == ()
    assert model.requests == []
    assert run_usage(stack.store, stack.run_id)["cost_usd"] == 0


@pytest.mark.asyncio
async def test_bundled_recursive_schema_with_root_and_relative_ids_is_accepted(
    stack: WorkflowStack,
) -> None:
    """A valid bundled recursive schema with an absolute root $id and embedded relative $id passes preflight."""
    bundled = {
        "$id": "https://example.org/schemas/tree.json",
        "$ref": "#/$defs/Node",
        "$defs": {
            "Node": {
                "$id": "node.json",
                "type": "object",
                "properties": {"child": {"$ref": "#"}},
            },
        },
    }
    check_schema(bundled)
    assert validation_error({"child": {}}, bundled) is None
    assert validation_error({"child": 3}, bundled) is not None

    model = stack.child_script(answer('```json\n{"child": {}}\n```'))
    assert await stack.result("@0", "agent", {"prompt": "Return a tree.", "schema": bundled}) == {"child": {}}
    assert len(model.requests) == 1

    # A dangling local reference inside the embedded relative-$id schema stays rejected.
    broken = {
        "$id": "https://example.org/schemas/tree.json",
        "$ref": "#/$defs/Node",
        "$defs": {
            "Node": {
                "$id": "node.json",
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Missing"}},
            },
        },
    }
    with pytest.raises(SchemaError) as missing:
        check_schema(broken)
    assert str(missing.value) == ("schema is not valid JSON Schema: reference '#/$defs/Missing' does not resolve.")
