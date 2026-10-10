"""Tests for authored subagent personas and workspace profile files."""

from __future__ import annotations

import json
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
    persona = inline_persona(prompt, ["file"])
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
        inline_persona(system_prompt, tools)


def test_inline_persona_accepts_prompt_at_size_limit() -> None:
    """A prompt of exactly 64 KiB is accepted."""
    assert len(inline_persona("x" * (64 * 1024), None).system_prompt) == 64 * 1024


def test_empty_tool_list_means_no_tools() -> None:
    """An explicit empty tool list keeps no tools instead of falling back to all of them."""
    persona = inline_persona("P", [])
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
        ),
        model="haiku",
        mode="minimal",
    )


def test_parse_profile_defaults_optional_fields() -> None:
    """A profile with only a description keeps every caller tool and normal model and mode selection."""
    profile = _parse_profile("plain", "---\ndescription: Plain.\n---\nBe brief.\n")
    assert profile.persona.tools is None
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
        "profile": None,
        "model": None,
        "minimal": False,
        "workspace_root": workspace,
        "available_toolkits": lambda: ["file", "shell"],
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

    plain = _resolve(tmp_path, system_prompt="", tools=[], profile="")
    profiled = _resolve(tmp_path, system_prompt="", tools=[], profile="critic")

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
    """A listing that would exceed 2,000 characters becomes a count pointing at subagents/."""
    entries = [
        _parse_profile(f"p{index:03d}", "---\ndescription: " + "d" * 40 + "\n---\nBody\n") for index in range(256)
    ]
    listing = render_profile_listing(entries)
    assert len(listing) <= 2000
    assert "256" in listing
    assert "subagents/" in listing


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
