"""Tests for authored subagent personas and workspace profile files."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools.function import Function

import mindroom.tools  # noqa: F401 - registers built-in tool metadata, including function names
from mindroom.delegation.personas import (
    PersonaError,
    PersonaRequest,
    _InvalidPersonaProfile,
    _parse_profile,
    _PersonaProfile,
    inline_persona,
    list_profiles,
    load_profile,
    persona_allows,
    persona_tool_policy,
    render_profile_listing,
    resolve_persona_request,
    validate_persona_tools,
)
from mindroom.delegation.state import SubagentPersona
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import (
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolFileAccess,
    ToolMetadata,
)

if TYPE_CHECKING:
    from pathlib import Path

_CRITIC = """---
description: Finds the three biggest risks.
tools: [file]
skills: [triage]
knowledge: [docs]
model: haiku
mode: minimal
---

You are a hostile reviewer.
Return three risks.
"""


def _profile_file(root: Path, name: str, content: str) -> None:
    directory = root / "subagents"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(content)


def _function(name: str, toolkit: str | None) -> Function:
    function = Function(name=name)
    function.owning_toolkit = toolkit
    return function


def test_inline_persona_keeps_prompt_bytes() -> None:
    """Braces and trailing whitespace survive validation and serialization unchanged."""
    prompt = "Use {braces} and {{x}}\n"
    persona = inline_persona(prompt, ["file"], None, None)
    assert persona == SubagentPersona(source_kind="inline", source_name="", system_prompt=prompt, tools=("file",))
    assert SubagentPersona.from_dict(json.loads(json.dumps(asdict(persona)))) == persona


@pytest.mark.parametrize(
    ("system_prompt", "tools"),
    [
        ("", None),
        ("x" * (64 * 1024 + 1), None),
        (None, None),
        ("ok", "file"),
        ("ok", [1]),
        ("ok", [""]),
    ],
)
def test_inline_persona_rejects_empty_oversized_and_wrong_types(system_prompt: object, tools: object) -> None:
    """Invalid prompts and tool lists raise a user-facing persona error."""
    with pytest.raises(PersonaError):
        inline_persona(system_prompt, tools, None, None)


def test_inline_persona_accepts_prompt_at_size_limit() -> None:
    """A prompt of exactly 64 KiB is accepted."""
    assert len(inline_persona("x" * (64 * 1024), None, None, None).system_prompt) == 64 * 1024


def test_empty_tool_list_means_no_tools() -> None:
    """An explicit empty tool list keeps no tools instead of falling back to all of them."""
    persona = inline_persona("P", [], None, None)
    assert persona.tools == ()
    assert not persona_allows((), "file", "read_file")
    _, disabled = persona_tool_policy(persona.tools, lambda: ["file", "shell"], None, frozenset())
    assert disabled == frozenset({"file", "shell", "dynamic_tools"})


def test_parse_profile_reads_frontmatter_and_body() -> None:
    """Frontmatter supplies tools, model, and mode, and the stripped body is the prompt."""
    profile = _parse_profile("critic", _CRITIC)
    assert profile == _PersonaProfile(
        name="critic",
        description="Finds the three biggest risks.",
        persona=SubagentPersona(
            source_kind="profile",
            source_name="critic",
            system_prompt="You are a hostile reviewer.\nReturn three risks.",
            tools=("file",),
            skills=("triage",),
            knowledge=("docs",),
        ),
        model="haiku",
        mode="minimal",
    )


def test_parse_profile_defaults_optional_fields() -> None:
    """A profile with only a description keeps every caller tool and normal model and mode selection."""
    profile = _parse_profile("plain", "---\ndescription: Plain.\n---\nBe brief.\n")
    assert profile.persona.tools is None
    assert profile.persona.skills is None
    assert profile.persona.knowledge is None
    assert profile.model is None
    assert profile.mode is None


@pytest.mark.parametrize(
    "content",
    [
        "---\ntools: [file]\n---\nBody\n",
        "---\ndescription: D\nrole: extra\n---\nBody\n",
        "---\ndescription: D\nmode: fast\n---\nBody\n",
        "---\ndescription: D\n---\n\n",
        "---\ndescription: " + "d" * 1025 + "\n---\nBody\n",
        "---\ndescription: D\nmodel: [a]\n---\nBody\n",
        "---\ndescription: D\n1: x\nrole: y\n---\nBody\n",
        "No frontmatter at all\n",
    ],
)
def test_parse_profile_rejects_bad_frontmatter(content: str) -> None:
    """Missing descriptions, unknown keys, bad modes, and empty bodies are invalid."""
    with pytest.raises(PersonaError):
        _parse_profile("critic", content)


def test_list_profiles_skips_unsafe_entries(tmp_path: Path) -> None:
    """Only regular lowercase-named Markdown files are read, and links are never followed."""
    _profile_file(tmp_path, "critic.md", _CRITIC)
    _profile_file(tmp_path, "broken.md", "---\ndescription: D\n---\n\n")
    _profile_file(tmp_path, "Critic.md", _CRITIC)
    _profile_file(tmp_path, "notes.txt", _CRITIC)
    (tmp_path / "subagents" / "dir.md").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(_CRITIC)
    (tmp_path / "subagents" / "link.md").symlink_to(outside)

    entries = list_profiles(tmp_path)

    assert [entry.name for entry in entries] == ["Critic", "broken", "critic"]
    assert [type(entry) for entry in entries] == [_InvalidPersonaProfile, _InvalidPersonaProfile, _PersonaProfile]
    assert "lowercase" in entries[0].reason


def test_listing_escapes_planted_filenames(tmp_path: Path) -> None:
    """A planted filename never puts undecodable bytes or a line break into the run_subagent description."""
    _profile_file(tmp_path, "critic.md", _CRITIC)
    _profile_file(tmp_path, os.fsdecode(b"\xffnotes.md"), _CRITIC)
    _profile_file(tmp_path, "two\nlines.md", _CRITIC)

    listing = render_profile_listing(list_profiles(tmp_path))

    listing.encode("utf-8")
    assert "- \\udcffnotes (invalid: " in listing
    assert "- two\\nlines (invalid: " in listing
    assert len(listing.splitlines()) == 3


def test_listing_keeps_each_profile_on_one_line(tmp_path: Path) -> None:
    """A multi-line description cannot add a fake entry to the run_subagent description."""
    _profile_file(tmp_path, "critic.md", '---\ndescription: "Finds risk.\\n- injected: fake profile"\n---\nBody\n')

    listing = render_profile_listing(list_profiles(tmp_path))

    assert listing == "- critic: Finds risk. - injected: fake profile"


def test_list_profiles_without_directory_is_empty(tmp_path: Path) -> None:
    """A workspace without subagents/ has no profiles."""
    assert list_profiles(tmp_path) == []


def test_list_profiles_refuses_linked_directory(tmp_path: Path) -> None:
    """A subagents/ symlink is not followed."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "critic.md").write_text(_CRITIC)
    (tmp_path / "subagents").symlink_to(real)
    assert list_profiles(tmp_path) == []


def test_list_profiles_caps_count_and_size(tmp_path: Path) -> None:
    """At most 256 profiles are read, sorted by name, and an oversized file is invalid."""
    for index in range(300):
        _profile_file(tmp_path, f"p{index:03d}.md", "---\ndescription: D\n---\nBody\n")
    entries = list_profiles(tmp_path)
    assert len(entries) == 256
    assert entries[0].name == "p000"
    assert entries[-1].name == "p255"

    big_root = tmp_path / "big"
    big_root.mkdir()
    _profile_file(big_root, "big.md", "---\ndescription: D\n---\n" + "x" * (64 * 1024))
    [entry] = list_profiles(big_root)
    assert isinstance(entry, _InvalidPersonaProfile)
    assert entry.name == "big"


def test_list_profiles_stops_at_its_total_budget(tmp_path: Path) -> None:
    """The listing stops reading once the profiles it read reach 1 MiB, while each profile still loads by name."""
    body = "x" * (60 * 1024)
    for index in range(20):
        _profile_file(tmp_path, f"p{index:02d}.md", f"---\ndescription: D\n---\n{body}\n")

    entries = list_profiles(tmp_path)

    assert 0 < len(entries) < 20
    assert sum(len(entry.persona.system_prompt) for entry in entries if isinstance(entry, _PersonaProfile)) <= 1 << 20
    assert load_profile(tmp_path, "p19").name == "p19"


def test_list_profiles_charges_unreadable_text_to_its_budget(tmp_path: Path) -> None:
    """A profile that is not UTF-8 still counts the bytes read against the listing budget."""
    directory = tmp_path / "subagents"
    directory.mkdir()
    for index in range(20):
        (directory / f"p{index:02d}.md").write_bytes(b"\xff" * (60 * 1024))

    entries = list_profiles(tmp_path)

    assert 0 < len(entries) < 20
    assert all(isinstance(entry, _InvalidPersonaProfile) for entry in entries)


def _resolve(workspace: Path, **arguments: object) -> PersonaRequest | str:
    """Resolve one ``run_subagent`` call by ``leader`` for itself, with file and shell as its tools."""
    defaults: dict[str, Any] = {
        "caller_name": "leader",
        "agent_name": "leader",
        "system_prompt": None,
        "tools": None,
        "skills": None,
        "knowledge": None,
        "profile": None,
        "model": None,
        "minimal": False,
        "workspace_root": workspace,
        "available_toolkits": lambda: ["file", "shell"],
        "available_skills": lambda: ["research", "writing"],
        "available_knowledge": lambda: ["docs", "faq"],
    }
    return resolve_persona_request(**{**defaults, **arguments})


def test_resolve_persona_request_mode_and_model_rules(tmp_path: Path) -> None:
    """Profiles supply model and mode; explicit arguments override them; plain calls pass through."""
    _profile_file(tmp_path, "fast.md", "---\ndescription: D\nmode: minimal\nmodel: haiku\n---\nBe quick.\n")
    _profile_file(tmp_path, "plain.md", "---\ndescription: D\n---\nBe careful.\n")

    fast = _resolve(tmp_path, profile="fast")
    plain_minimal = _resolve(tmp_path, profile="plain", model="sonnet", minimal=True)

    assert isinstance(fast, PersonaRequest)
    assert fast.persona is not None
    assert (fast.model, fast.agent_mode, fast.persona.system_prompt) == ("haiku", "minimal", "Be quick.")
    assert isinstance(plain_minimal, PersonaRequest)
    assert (plain_minimal.model, plain_minimal.agent_mode) == ("sonnet", "minimal")
    assert _resolve(tmp_path, minimal=True) == PersonaRequest(persona=None, model=None, agent_mode="minimal")
    assert _resolve(None, profile="fast") == "Cannot delegate: subagent profiles need an agent workspace."


def test_empty_authoring_arguments_mean_a_plain_copy_or_the_named_profile(tmp_path: Path) -> None:
    """A model that fills every optional argument with empty values starts a plain copy, or runs its profile."""
    _profile_file(tmp_path, "critic.md", "---\ndescription: Critic.\ntools: [file]\n---\nCritic prompt.\n")

    plain = _resolve(tmp_path, system_prompt="", tools=[], skills=[], knowledge=[], profile="")
    profiled = _resolve(tmp_path, system_prompt="", tools=[], skills=[], knowledge=[], profile="critic")

    assert plain == PersonaRequest(persona=None, model=None, agent_mode="standard")
    assert isinstance(profiled, PersonaRequest)
    assert profiled.persona is not None
    assert (profiled.persona.source_name, profiled.persona.tools) == ("critic", ("file",))


def test_minimal_persona_with_tools_must_keep_shell(tmp_path: Path) -> None:
    """A minimal persona that lists its tools is refused up front unless it keeps shell."""
    refused = _resolve(tmp_path, system_prompt="P", tools=["file"], minimal=True)

    assert refused == "Cannot delegate: a minimal subagent needs shell among its tools."
    assert isinstance(_resolve(tmp_path, system_prompt="P", tools=["file", "shell"], minimal=True), PersonaRequest)
    assert isinstance(_resolve(tmp_path, system_prompt="P", minimal=True), PersonaRequest)


def test_load_profile_reads_one_file(tmp_path: Path) -> None:
    """A named profile loads from subagents/<name>.md."""
    _profile_file(tmp_path, "critic.md", _CRITIC)
    assert load_profile(tmp_path, "critic").persona.system_prompt.startswith("You are a hostile reviewer.")


def test_load_profile_missing_raises(tmp_path: Path) -> None:
    """A missing profile names the subagents/ directory."""
    with pytest.raises(PersonaError) as error:
        load_profile(tmp_path, "absent")
    assert str(error.value) == "Cannot delegate: subagent profile 'absent' was not found in subagents/."


def test_load_profile_invalid_reports_reason(tmp_path: Path) -> None:
    """An invalid profile names itself and the reason."""
    _profile_file(tmp_path, "broken.md", "---\ndescription: D\n---\n\n")
    with pytest.raises(PersonaError) as error:
        load_profile(tmp_path, "broken")
    assert str(error.value).startswith("Cannot delegate: subagent profile 'broken' is invalid: ")


@pytest.mark.parametrize("name", ["Critic", "../escape", "", "a" * 65, "with space"])
def test_load_profile_rejects_bad_names(tmp_path: Path, name: str) -> None:
    """Names outside the profile pattern are refused before any file access."""
    with pytest.raises(PersonaError):
        load_profile(tmp_path, name)


def test_render_profile_listing_shows_entries_and_errors() -> None:
    """Valid profiles show their description and invalid ones their reason."""
    listing = render_profile_listing(
        [
            _InvalidPersonaProfile(name="broken", reason="empty body"),
            _parse_profile("critic", _CRITIC),
        ],
    )
    assert "broken (invalid: empty body)" in listing
    assert "critic: Finds the three biggest risks." in listing


def test_render_profile_listing_bounds_length() -> None:
    """A listing that would exceed 2,000 characters becomes a pointer at subagents/, with no count past the read limit."""
    entries = [
        _parse_profile(f"p{index:03d}", "---\ndescription: " + "d" * 40 + "\n---\nBody\n") for index in range(256)
    ]
    listing = render_profile_listing(entries)
    assert listing == "Too many subagent profiles to list are saved in subagents/; list that directory to see them."


def test_render_profile_listing_empty() -> None:
    """No profiles render no listing."""
    assert render_profile_listing([]) == ""


def test_validate_persona_tools_accepts_subset_and_function_entries() -> None:
    """Toolkit and toolkit.function entries pass when the caller has the toolkit."""
    validate_persona_tools(("file", "gmail.search_emails"), ["file", "gmail"])
    validate_persona_tools(None, ["file"])
    validate_persona_tools((), [])


@pytest.mark.parametrize("entry", ["shell", "gmail.send_email_x", "gmail.", ".search_emails"])
def test_validate_persona_tools_rejects_unknown_toolkit_and_function(entry: str) -> None:
    """Entries outside the caller's toolkits, or unknown functions, fail with the available names."""
    with pytest.raises(PersonaError) as error:
        validate_persona_tools((entry,), ["file", "gmail"])
    assert str(error.value) == f"Cannot delegate: unknown tool '{entry}'. Your tools: file, gmail."


def test_persona_allows_whole_toolkits_and_single_functions() -> None:
    """Toolkit entries keep every function of that concrete toolkit; function entries keep one."""
    entries = ("gmail.search_emails", "file")
    assert persona_allows(entries, "file", "read_file")
    assert persona_allows(entries, "gmail", "search_emails")
    assert not persona_allows(entries, "gmail", "send_email")
    assert not persona_allows(entries, "shell", "run_shell_command")


def test_persona_tool_policy_narrows_only_an_explicit_tool_list() -> None:
    """An explicit list hides generated functions, keeps the caller's filter, and skips unnamed toolkits."""

    def caller_filter(function: Function) -> bool:
        return function.name != "write_file"

    visible, disabled = persona_tool_policy(
        ("gmail.search_emails", "file"),
        lambda: ["file", "gmail", "shell"],
        caller_filter,
        frozenset({"memory"}),
    )

    assert visible is not None
    assert visible(_function("read_file", "file"))
    assert not visible(_function("write_file", "file"))
    assert not visible(_function("generated", None))
    assert visible(_function("get_skill_instructions", None))
    assert disabled == frozenset({"memory", "shell", "dynamic_tools"})
    assert persona_tool_policy(None, lambda: ["file"], caller_filter, frozenset()) == (caller_filter, frozenset())


def test_function_level_cap_admits_only_named_functions() -> None:
    """A cap that names one function admits that function, not its whole toolkit or a sibling."""
    cap = ("gmail.search_emails", "file")
    validate_persona_tools(("gmail.search_emails", "file"), ["gmail", "file"], cap)
    for entry in ("gmail", "gmail.send_email", "calculator"):
        with pytest.raises(PersonaError) as error:
            validate_persona_tools((entry,), ["gmail", "file", "calculator"], cap)
        assert str(error.value) == f"Cannot delegate: unknown tool '{entry}'. Your tools: gmail.search_emails, file."


def test_mcp_function_entries_are_not_checked_against_bridge_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """An MCP toolkit learns its functions when built, so a function entry for it is accepted by name."""
    monkeypatch.setitem(
        TOOL_METADATA,
        "mcp_demo",
        ToolMetadata(
            name="mcp_demo",
            display_name="Demo",
            description="Demo MCP server",
            category=ToolCategory.DEVELOPMENT,
            file_access=ToolFileAccess.NONE,
            authored_override_validator=ToolAuthoredOverrideValidator.MCP,
            function_names=("mcp_demo_connect",),
        ),
    )
    validate_persona_tools(("mcp_demo.demo_echo",), ["mcp_demo"])
    with pytest.raises(PersonaError):
        validate_persona_tools(("mcp_other.demo_echo",), ["mcp_demo"])


def test_inherited_tools_drop_only_delegate_the_copy_cannot_have(tmp_path: Path) -> None:
    """A copy without tools inherits its parent's cap minus delegate at the maximum depth, and refuses any other loss."""
    cap = inline_persona("P", ["delegate", "file"], None, None)
    at_max_depth = _resolve(tmp_path, system_prompt="Q", cap=cap, available_toolkits=lambda: ["file"])
    lost_file = _resolve(tmp_path, system_prompt="Q", cap=cap, available_toolkits=lambda: ["delegate"])

    assert isinstance(at_max_depth, PersonaRequest)
    assert at_max_depth.persona is not None
    assert at_max_depth.persona.tools == ("file",)
    assert lost_file == "Cannot delegate: unknown tool 'file'. Your tools: delegate."


def test_inline_persona_reads_skills() -> None:
    """Skills are optional, deduplicated names, and an empty list means no skills."""
    assert inline_persona("P", None, None, None).skills is None
    assert inline_persona("P", None, [" research ", "research", "writing"], None).skills == ("research", "writing")
    assert inline_persona("P", None, [], None).skills == ()
    for skills in ("research", [""], [1]):
        with pytest.raises(PersonaError, match="skills must be a list of skill names"):
            inline_persona("P", None, skills, None)


def test_authored_skills_must_be_caller_skills(tmp_path: Path) -> None:
    """A skill the caller lacks is refused, and the caller's skills are read only when the persona names some."""

    def unread() -> list[str]:
        pytest.fail("the caller's skills were read for a persona naming none")

    subset = _resolve(tmp_path, system_prompt="P", skills=["writing"])
    unknown = _resolve(tmp_path, system_prompt="P", skills=["coding"])

    assert isinstance(subset, PersonaRequest)
    assert subset.persona is not None
    assert subset.persona.skills == ("writing",)
    assert unknown == "Cannot delegate: unknown skill 'coding'. Your skills: research, writing."
    assert isinstance(_resolve(tmp_path, system_prompt="P", available_skills=unread), PersonaRequest)
    assert isinstance(_resolve(tmp_path, system_prompt="P", skills=[], available_skills=unread), PersonaRequest)


def test_profile_cannot_combine_with_inline_skills(tmp_path: Path) -> None:
    """A profile carries its own skills, so inline skills beside it are refused."""
    _profile_file(tmp_path, "plain.md", "---\ndescription: D\n---\nBe careful.\n")

    refused = _resolve(tmp_path, profile="plain", skills=["research"])

    assert refused == "Cannot delegate: pass either profile or system_prompt, tools, skills, and knowledge, not both."


def test_copy_without_skills_inherits_its_parents_skills(tmp_path: Path) -> None:
    """An authored subagent's skills cap the copies it authors, and a copy without skills inherits them."""
    cap = inline_persona("P", None, ["research"], None)

    inherited = _resolve(tmp_path, system_prompt="Q", cap=cap)
    widened = _resolve(tmp_path, system_prompt="Q", skills=["writing"], cap=cap)
    plain = _resolve(tmp_path, cap=cap)

    assert isinstance(inherited, PersonaRequest)
    assert inherited.persona is not None
    assert (inherited.persona.tools, inherited.persona.skills) == (None, ("research",))
    assert widened == "Cannot delegate: unknown skill 'writing'. Your skills: research."
    assert isinstance(plain, str)
    assert "pass system_prompt or profile" in plain


def test_unrestricted_authored_subagent_may_start_a_configured_copy(tmp_path: Path) -> None:
    """An authored subagent that keeps every caller tool and skill may still start a configured copy."""
    plain = _resolve(tmp_path, cap=inline_persona("P", None, None, None))

    assert plain == PersonaRequest(persona=None, model=None, agent_mode="standard")


def test_persona_naming_tools_but_no_skills_gets_none(tmp_path: Path) -> None:
    """Skills follow an explicit tool list, as knowledge search does: naming tools without skills leaves none."""

    def unread() -> list[str]:
        pytest.fail("the caller's skills were read for a persona naming none")

    named_tools = _resolve(tmp_path, system_prompt="P", tools=["file"], available_skills=unread)
    neither = _resolve(tmp_path, system_prompt="P", available_skills=unread)
    copy_with_tools = _resolve(
        tmp_path,
        system_prompt="Q",
        tools=["file"],
        cap=inline_persona("P", None, ["research"], None),
        available_skills=unread,
    )

    for request, skills in ((named_tools, ()), (neither, None), (copy_with_tools, ())):
        assert isinstance(request, PersonaRequest)
        assert request.persona is not None
        assert request.persona.skills == skills


def test_inherited_skills_keep_only_those_the_caller_still_has(tmp_path: Path) -> None:
    """A copy that inherits skills drops one its caller lost, as a follow-up would, while naming that skill refuses."""
    cap = inline_persona("P", None, ["research", "retired"], None)

    inherited = _resolve(tmp_path, system_prompt="Q", cap=cap)
    named = _resolve(tmp_path, system_prompt="Q", skills=["retired"], cap=cap)

    assert isinstance(inherited, PersonaRequest)
    assert inherited.persona is not None
    assert inherited.persona.skills == ("research",)
    assert named == "Cannot delegate: unknown skill 'retired'. Your skills: research."


def test_cap_from_a_parent_listing_only_tools_admits_no_skills(tmp_path: Path) -> None:
    """A parent that lists tools but no skills keeps none, so a copy it authors cannot name any."""
    cap = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",))

    refused = _resolve(tmp_path, system_prompt="Q", skills=["research"], cap=cap)

    assert refused == "Cannot delegate: unknown skill 'research'. Your skills: none."


def test_cap_from_a_parent_listing_only_tools_admits_no_knowledge(tmp_path: Path) -> None:
    """A parent that lists tools but no knowledge searches none, so a copy it authors cannot name any."""
    cap = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",))

    refused = _resolve(tmp_path, system_prompt="Q", knowledge=["docs"], cap=cap)

    assert refused == "Cannot delegate: unknown knowledge base 'docs'. Your knowledge bases: none."


def test_copy_that_omits_everything_inherits_its_parents_tools_skills_and_knowledge(tmp_path: Path) -> None:
    """Inheriting the parent's tools must not turn its inherited skills and knowledge into an empty explicit list."""
    cap = SubagentPersona(
        source_kind="inline",
        source_name="",
        system_prompt="P",
        tools=("file",),
        skills=("research",),
        knowledge=("docs",),
    )

    request = _resolve(tmp_path, system_prompt="Q", cap=cap)

    assert isinstance(request, PersonaRequest)
    assert request.persona is not None
    assert (request.persona.tools, request.persona.skills, request.persona.knowledge) == (
        ("file",),
        ("research",),
        ("docs",),
    )


def test_copy_of_a_parent_searching_no_knowledge_searches_none(tmp_path: Path) -> None:
    """A parent that names no knowledge bases passes that on to a copy that omits knowledge."""
    cap = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", knowledge=())

    request = _resolve(tmp_path, system_prompt="Q", cap=cap)

    assert isinstance(request, PersonaRequest)
    assert request.persona is not None
    assert request.persona.knowledge == ()


def test_knowledge_without_a_prompt_or_profile_is_refused(tmp_path: Path) -> None:
    """Naming knowledge bases authors a copy, so it needs a prompt instead of starting a configured copy."""
    assert _resolve(tmp_path, knowledge=["docs"]) == "Cannot delegate: system_prompt must be a non-empty string."


def test_inline_persona_reads_knowledge() -> None:
    """Knowledge bases are optional, deduplicated names, and an empty list means no knowledge search."""
    assert inline_persona("P", None, None, None).knowledge is None
    assert inline_persona("P", None, None, [" docs ", "docs", "faq"]).knowledge == ("docs", "faq")
    assert inline_persona("P", None, None, []).knowledge == ()
    for knowledge in ("docs", [""], [1]):
        with pytest.raises(PersonaError, match="knowledge must be a list of knowledge base names"):
            inline_persona("P", None, None, knowledge)


def test_knowledge_follows_an_explicit_tool_list() -> None:
    """Like skills, a persona that lists tools but no knowledge bases keeps none, and one that lists neither keeps all."""
    named_tools = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",))
    neither = SubagentPersona(source_kind="inline", source_name="", system_prompt="P")
    named = SubagentPersona(
        source_kind="inline",
        source_name="",
        system_prompt="P",
        tools=("file",),
        knowledge=("docs",),
    )

    assert (named_tools.kept_knowledge, neither.kept_knowledge, named.kept_knowledge) == ((), None, ("docs",))


def test_authored_knowledge_must_be_caller_knowledge(tmp_path: Path) -> None:
    """A knowledge base the caller lacks is refused, and the caller's bases are read only when the persona names some."""

    def unread() -> list[str]:
        pytest.fail("the caller's knowledge bases were read for a persona naming none")

    subset = _resolve(tmp_path, system_prompt="P", tools=["file"], knowledge=["faq"])
    unknown = _resolve(tmp_path, system_prompt="P", knowledge=["wiki"])

    assert isinstance(subset, PersonaRequest)
    assert subset.persona is not None
    assert subset.persona.knowledge == ("faq",)
    assert unknown == "Cannot delegate: unknown knowledge base 'wiki'. Your knowledge bases: docs, faq."
    assert isinstance(_resolve(tmp_path, system_prompt="P", available_knowledge=unread), PersonaRequest)


def test_copy_inherits_its_parents_knowledge_and_stays_within_it(tmp_path: Path) -> None:
    """An authored subagent's knowledge bases cap the copies it authors, and a copy naming neither list inherits them."""
    cap = inline_persona("P", None, None, ["docs"])

    inherited = _resolve(tmp_path, system_prompt="Q", cap=cap)
    widened = _resolve(tmp_path, system_prompt="Q", knowledge=["faq"], cap=cap)
    plain = _resolve(tmp_path, cap=cap)

    assert isinstance(inherited, PersonaRequest)
    assert inherited.persona is not None
    assert inherited.persona.knowledge == ("docs",)
    assert widened == "Cannot delegate: unknown knowledge base 'faq'. Your knowledge bases: docs."
    assert isinstance(plain, str)
    assert "pass system_prompt or profile" in plain


def test_profile_cannot_combine_with_inline_knowledge(tmp_path: Path) -> None:
    """A profile carries its own knowledge bases, so inline knowledge beside it is refused."""
    _profile_file(tmp_path, "plain.md", "---\ndescription: D\n---\nBe careful.\n")

    refused = _resolve(tmp_path, profile="plain", knowledge=["docs"])

    assert refused == "Cannot delegate: pass either profile or system_prompt, tools, skills, and knowledge, not both."


def test_persona_tool_policy_keeps_knowledge_search() -> None:
    """An explicit tool list still offers knowledge search, which loads only when the persona keeps a base."""
    visible, _disabled = persona_tool_policy(("file",), lambda: ["file"], None, frozenset())

    assert visible is not None
    assert visible(_function("search_knowledge_base", None))
