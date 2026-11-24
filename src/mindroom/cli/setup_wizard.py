"""Interactive starter setup wizard, provider preset catalog, and model selection helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from rich.markup import escape

from mindroom import constants
from mindroom.cli.interactive import Separator, confirm, secret, select
from mindroom.model_defaults import (
    CONFIG_INIT_ADDITIONAL_MODELS,
    CONFIG_INIT_HELPER_MODELS,
    CONFIG_INIT_MODEL_PRESETS,
    ModelPreset,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from rich.console import Console

    from mindroom.cli.setup_detection import DetectedProvider, DetectionSnapshot, OllamaModelInfo
    from mindroom.constants import RuntimePaths

__all__ = [
    "PROVIDER_CHOICES",
    "PROVIDER_PRESETS",
    "PresetInfo",
    "ProviderMenuChoice",
    "ProviderPreset",
    "SetupHints",
    "StarterModelSelection",
    "WizardIO",
    "default_model_selection",
    "normalize_provider_preset",
    "preset_api_key_env",
    "print_setup_summary",
    "prompt_provider_choice",
    "prompt_provider_preset",
    "resolve_interactive_setup",
]

ProviderPreset = Literal[
    "anthropic",
    "bedrock_claude",
    "azure",
    "claude_code",
    "codex",
    "kimi",
    "llama_cpp",
    "ollama",
    "openai",
    "openrouter",
    "vertexai_claude",
]
ProviderMenuChoice = ProviderPreset | Literal["claude_code+codex"]
CombinedDefaultChoice = Literal["sonnet", "sol", "opus"]

_YAML_RESERVED_KEYS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "y", "n"})


class _SelectFn(Protocol):
    def __call__[Value: str](self, message: str, choices: Sequence[tuple[Value, str] | Separator]) -> Value: ...


class _ConfirmFn(Protocol):
    def __call__(self, message: str, *, default: bool) -> bool: ...


class _PrintLiteralFn(Protocol):
    def __call__(self, markup: str, text: object, suffix: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class WizardIO:
    """Console and interactive prompt callables used by the setup wizard."""

    console: Console
    print_literal: _PrintLiteralFn
    select: _SelectFn = select
    confirm: _ConfirmFn = confirm
    secret: Callable[[str], str] = secret


@dataclass(frozen=True)
class StarterModelSelection:
    """Resolved model layout and readiness for starter config and `.env` generation."""

    primary_preset: ProviderPreset
    default_model: ModelPreset
    additional_models: tuple[tuple[str, ModelPreset], ...] = ()
    helper_model: str | None = None
    commented_alternatives_preset: ProviderPreset | None = None
    combined_claude_codex: bool = False
    claude_cli_path: str | None = None
    uses_detected_ollama_models: bool = False
    ready: bool = False


@dataclass(frozen=True)
class SetupHints:
    """What a preset needs before agents can answer, worded for each place setup is described."""

    # First-run setup keeps starting MindRoom, which reads `.env` only at startup, so `.env` steps end in a restart.
    first_run: str
    # `config init` next steps for the hosted (mindroom.chat) and self-hosted Matrix presets.
    hosted: str
    self_hosted: str


@dataclass(frozen=True)
class PresetInfo:
    """One provider preset: its menu entry, the extra names `--provider` accepts, and its setup hints."""

    label: str
    needs: str
    aliases: tuple[str, ...] = ()
    # The `--provider` value help lists, when it is one of the aliases rather than the preset name.
    choice: str | None = None
    # None means the preset needs exactly one API key, which first-run setup asks for.
    setup: SetupHints | None = None


def _login_hints(command: str) -> SetupHints:
    """Describe the same login step for each setup path."""
    return SetupHints(
        first_run=f"Run {command} in another terminal",
        hosted=f"Run {command} before starting MindRoom (Matrix homeserver is prefilled)",
        self_hosted=f"Set your Matrix homeserver and run {command} before starting MindRoom",
    )


# Every provider preset in menu order, default first.
PROVIDER_PRESETS: dict[ProviderPreset, PresetInfo] = {
    "openai": PresetInfo("OpenAI", "API key"),
    "anthropic": PresetInfo("Anthropic", "API key", aliases=("claude",)),
    "openrouter": PresetInfo("OpenRouter", "API key"),
    "codex": PresetInfo(
        "Codex",
        "ChatGPT sign-in through `codex login`",
        setup=_login_hints("`codex login`"),
    ),
    "claude_code": PresetInfo(
        "Claude subscription",
        "Claude sign-in through `claude auth login`",
        setup=_login_hints("`claude auth login`"),
    ),
    "kimi": PresetInfo(
        "Kimi Code",
        "Kimi sign-in through `kimi`",
        setup=_login_hints("`kimi` and `/login`"),
    ),
    "ollama": PresetInfo(
        "Ollama",
        "Local models served by Ollama",
        setup=SetupHints(
            first_run="Start Ollama and pull the local models",
            hosted="Start Ollama and pull the local models (Matrix homeserver is prefilled)",
            self_hosted="Set your Matrix homeserver, start Ollama, and pull the local models",
        ),
    ),
    "llama_cpp": PresetInfo(
        "llama.cpp",
        "Local models served by llama.cpp",
        aliases=("llama.cpp", "llama-cpp"),
        choice="llama.cpp",
        setup=SetupHints(
            first_run="Start llama.cpp server with the local model",
            hosted="Start llama.cpp server with the local model (Matrix homeserver is prefilled)",
            self_hosted="Set your Matrix homeserver and start llama.cpp server with the local model",
        ),
    ),
    "azure": PresetInfo(
        "Azure OpenAI",
        "Azure OpenAI key and endpoint in .env",
        aliases=("azure_openai", "azure-openai"),
        setup=SetupHints(
            first_run="Set your Azure OpenAI key and endpoint in {env_path}, confirm the config model deployment name, "
            "and restart `mindroom run`",
            hosted="Set your Azure OpenAI key/endpoint and confirm the config model deployment name",
            self_hosted="Set your Matrix homeserver, Azure OpenAI key/endpoint, and config model deployment name",
        ),
    ),
    "bedrock_claude": PresetInfo(
        "AWS Bedrock",
        "Claude on AWS Bedrock, AWS credentials in .env",
        setup=SetupHints(
            first_run="Set your AWS Bedrock region and credentials in {env_path} and restart `mindroom run`",
            hosted="Set AWS Bedrock region and credentials (Matrix homeserver is prefilled)",
            self_hosted="Set your Matrix homeserver, AWS Bedrock region, and AWS credentials",
        ),
    ),
    "vertexai_claude": PresetInfo(
        "Vertex AI",
        "Claude on Vertex AI, Google Cloud project in .env",
        aliases=("vertexai", "vertex", "vertexai-anthropic", "vertex-anthropic"),
        setup=SetupHints(
            first_run="Set your Vertex AI project and region in {env_path}, sign in with Google auth, "
            "and restart `mindroom run`",
            hosted="Set your Vertex AI project/region and Google auth (Matrix homeserver is prefilled)",
            self_hosted="Set your Matrix homeserver, Vertex AI project/region, and Google auth",
        ),
    ),
}
_CANONICAL_PRESETS: dict[str, ProviderPreset] = {preset: preset for preset in PROVIDER_PRESETS}
_PROVIDER_PRESET_NAMES: dict[str, ProviderPreset] = {
    name: preset for preset, info in PROVIDER_PRESETS.items() for name in (preset, *info.aliases)
}
PROVIDER_CHOICES = tuple(sorted(info.choice or preset for preset, info in PROVIDER_PRESETS.items()))


def normalize_provider_preset(provider: str) -> ProviderPreset | None:
    """Normalize provider preset values used by prompts and CLI flags."""
    return _PROVIDER_PRESET_NAMES.get(provider.strip().lower())


def preset_api_key_env(provider_preset: ProviderPreset) -> str | None:
    """Return the API key env var of the preset's model provider, if it has one."""
    return constants.env_key_for_provider(CONFIG_INIT_MODEL_PRESETS[provider_preset].provider)


def default_model_selection(
    preset: ProviderPreset,
    *,
    claude_cli_path: str | None = None,
    ready: bool = False,
) -> StarterModelSelection:
    """Build the standard single-provider starter model selection."""
    return StarterModelSelection(
        primary_preset=preset,
        default_model=CONFIG_INIT_MODEL_PRESETS[preset],
        additional_models=CONFIG_INIT_ADDITIONAL_MODELS.get(preset, ()),
        helper_model=CONFIG_INIT_HELPER_MODELS.get(preset),
        commented_alternatives_preset=preset,
        claude_cli_path=claude_cli_path if preset == "claude_code" else None,
        ready=ready,
    )


def _combined_claude_codex_selection(
    default_choice: CombinedDefaultChoice,
    *,
    claude_cli_path: str | None,
    ready: bool,
) -> StarterModelSelection:
    """Build the combined 6-model Claude Code + Codex starter selection without duplicating default."""
    claude_extra = dict(CONFIG_INIT_ADDITIONAL_MODELS["claude_code"])
    codex_extra = dict(CONFIG_INIT_ADDITIONAL_MODELS["codex"])
    all_models: tuple[tuple[str, ModelPreset], ...] = (
        ("sonnet", CONFIG_INIT_MODEL_PRESETS["claude_code"]),
        ("opus", claude_extra["opus"]),
        ("haiku", claude_extra["haiku"]),
        ("sol", CONFIG_INIT_MODEL_PRESETS["codex"]),
        ("astra", codex_extra["astra"]),
        ("luna", codex_extra["luna"]),
    )
    by_name = dict(all_models)
    additional = tuple((name, preset) for name, preset in all_models if name != default_choice)
    is_codex_default = default_choice == "sol"
    return StarterModelSelection(
        primary_preset="codex" if is_codex_default else "claude_code",
        default_model=by_name[default_choice],
        additional_models=additional,
        helper_model="luna" if is_codex_default else "haiku",
        combined_claude_codex=True,
        claude_cli_path=claude_cli_path,
        ready=ready,
    )


def _ollama_model_key(model_name: str, used: set[str]) -> str:
    """Return a clean, unique YAML model key for a pulled Ollama model name."""
    base = model_name.removesuffix(":latest")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", base.lower()).strip("_") or "model"
    if not slug[0].isalpha() or slug in _YAML_RESERVED_KEYS:
        slug = f"ollama_{slug}"
    candidate = slug
    suffix = 2
    while candidate in used:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _ollama_model_selection(chosen_name: str, models: tuple[OllamaModelInfo, ...]) -> StarterModelSelection:
    """Build an Ollama starter selection from already-pulled local models."""
    ctx = CONFIG_INIT_MODEL_PRESETS["ollama"].context_window
    used_keys = {"default"}
    extras = tuple(
        (_ollama_model_key(model.name, used_keys), ModelPreset("ollama", model.name, ctx))
        for model in [model for model in models if model.name != chosen_name][:2]
    )
    return StarterModelSelection(
        primary_preset="ollama",
        default_model=ModelPreset("ollama", chosen_name, ctx),
        additional_models=extras,
        uses_detected_ollama_models=True,
        ready=True,
    )


def _detected_provider_badge(detected: DetectedProvider) -> str:
    """Format the status column for one detected provider in the interactive menu."""
    from mindroom.cli.setup_detection import inline_cli_login_command  # noqa: PLC0415

    if detected.ready:
        if detected.preset in {"claude_code", "codex", "kimi"}:
            return "✓ signed in" if detected.status_detail == "signed in" else f"✓ signed in ({detected.status_detail})"
        if detected.preset == "ollama":
            return f"✓ running ({detected.status_detail})"
        return f"✓ {detected.status_detail}"
    if detected.cli_path is not None:
        if inline_cli_login_command(detected.preset) is not None:
            return "○ CLI installed · sign in now"
        return "○ CLI installed"
    return f"○ {detected.status_detail}"


def prompt_provider_preset(*, select_fn: _SelectFn = select) -> ProviderPreset:
    """Ask for a starter provider preset in default menu order without detection."""
    width = max(len(info.label) for info in PROVIDER_PRESETS.values()) + 2
    return select_fn(
        "Which model provider should your agents use?",
        [(preset, f"{info.label:<{width}}{info.needs}") for preset, info in PROVIDER_PRESETS.items()],
    )


def prompt_provider_choice(snapshot: DetectionSnapshot, *, select_fn: _SelectFn = select) -> ProviderMenuChoice:
    """Show the starter provider menu, ordering detected ready/installed providers first."""
    if not snapshot.providers:
        return prompt_provider_preset(select_fn=select_fn)

    width = max(len("Claude + Codex"), *(len(info.label) for info in PROVIDER_PRESETS.values())) + 2
    choices: list[tuple[ProviderMenuChoice, str] | Separator] = []
    listed: set[ProviderPreset] = set()

    if snapshot.both_claude_and_codex_ready():
        choices.append(("claude_code+codex", f"{'Claude + Codex':<{width}}recommended · both signed in"))

    for is_ready in (True, False):
        for preset_name, detected in snapshot.providers.items():
            if detected.ready is not is_ready or (preset := _CANONICAL_PRESETS.get(preset_name)) is None:
                continue
            label = PROVIDER_PRESETS[preset].label
            choices.append((preset, f"{label:<{width}}{_detected_provider_badge(detected)}"))
            listed.add(preset)

    remaining = [
        (preset, f"{info.label:<{width}}{info.needs}")
        for preset, info in PROVIDER_PRESETS.items()
        if preset not in listed
    ]
    if choices and remaining:
        choices.append(Separator("Other providers"))
    choices.extend(remaining)
    return select_fn("Which model provider should your agents use?", choices)


def print_setup_summary(selection: StarterModelSelection, *, io: WizardIO) -> None:
    """Print a compact summary of configured models, router, and provider readiness."""
    default_label = selection.default_model.display_name or selection.default_model.id
    models = [f"default ({default_label})", *(name for name, _ in selection.additional_models)]
    router_model = selection.helper_model or "default"
    header = "Setup ready" if selection.ready else "Setup created"
    status = "[green]ready[/green]" if selection.ready else "[yellow]needs setup[/yellow]"
    io.console.print(f"\n[bold]{header}[/bold]")
    io.print_literal("  Models: ", ", ".join(models))
    io.console.print(f"  Router: {router_model}")
    io.console.print(f"  Status: {status}")


def _print_skipped_key_hint(env_key: str, env_path: Path, *, for_config_init: bool, io: WizardIO) -> None:
    if for_config_init:
        io.print_literal(f"Skipped. Add {env_key} to ", env_path, " before starting MindRoom.")
        return
    io.print_literal(
        f"Skipped. Connect your provider in the dashboard once MindRoom is running, or add {env_key} to ",
        env_path,
        " and restart `mindroom run`.",
    )


def _prompt_provider_key(
    selected_preset: ProviderPreset,
    runtime_paths: RuntimePaths,
    *,
    interactive: bool,
    for_config_init: bool = False,
    io: WizardIO,
) -> tuple[str | None, bool]:
    """Ask a terminal for the preset's API key with live verification; return `(typed_key, ready)`."""
    from mindroom.credentials.environment import get_secret_from_env  # noqa: PLC0415

    preset = PROVIDER_PRESETS[selected_preset]
    env_key = preset_api_key_env(selected_preset)
    if preset.setup is not None or env_key is None:
        return None, False
    if get_secret_from_env(env_key, runtime_paths):
        io.console.print(f"Using {env_key} from your environment.")
        return None, True
    if not interactive:
        _print_skipped_key_hint(env_key, runtime_paths.env_path, for_config_init=for_config_init, io=io)
        return None, False

    from mindroom.cli.setup_detection import verify_provider_api_key  # noqa: PLC0415

    while True:
        value = io.secret(f"{preset.label} API key (input hidden, press Enter to skip)").strip()
        if not value:
            _print_skipped_key_hint(env_key, runtime_paths.env_path, for_config_init=for_config_init, io=io)
            return None, False
        with io.console.status(f"Verifying {preset.label} API key..."):
            check = verify_provider_api_key(selected_preset, value, runtime_paths=runtime_paths)
        if check.status == "valid":
            io.console.print(f"[green]✓[/green] Verified key with {preset.label}.")
            return value, True
        if check.status == "rejected":
            reason = f" ({escape(check.detail)})" if check.detail else ""
            io.console.print(
                f"[red]✗[/red] {preset.label} rejected that API key{reason}. Try again, or press Enter to skip.",
            )
            continue
        if check.custom_endpoint:
            io.console.print(f"[yellow]{escape(check.detail)} Saving anyway.[/yellow]")
        else:
            io.console.print("[yellow]Could not verify key right now; saving anyway.[/yellow]")
        return value, True


def _maybe_run_inline_cli_login(
    preset: ProviderPreset,
    detected: DetectedProvider | None,
    runtime_paths: RuntimePaths,
    *,
    io: WizardIO,
) -> DetectedProvider | None:
    """Offer to run `codex login` or `claude auth login` inline when the CLI is installed but signed out."""
    from mindroom.cli.setup_detection import (  # noqa: PLC0415
        detect_single_provider,
        inline_cli_login_command,
        run_inline_cli_login,
    )

    if detected is None or detected.ready or detected.cli_path is None:
        return detected
    cmd = inline_cli_login_command(preset)
    if cmd is None or not io.confirm(f"Not signed in yet. Run `{cmd}` now?", default=True):
        return detected
    if not run_inline_cli_login(preset, detected.cli_path, runtime_paths=runtime_paths):
        io.console.print(f"[yellow]`{cmd}` did not finish signing in; you can run it later.[/yellow]")
        return detected
    refreshed = detect_single_provider(preset, runtime_paths)
    if refreshed is not None:
        detected = refreshed
    if detected.ready:
        io.console.print(
            f"[green]✓[/green] Signed in to {PROVIDER_PRESETS[preset].label} ({escape(detected.status_detail)}).",
        )
    else:
        io.console.print(f"[yellow]Still not signed in; you can run `{cmd}` later.[/yellow]")
    return detected


def resolve_interactive_setup(
    runtime_paths: RuntimePaths,
    *,
    explicit_preset: ProviderPreset | None,
    interactive: bool,
    for_config_init: bool = False,
    io: WizardIO,
) -> tuple[StarterModelSelection, str | None]:
    """Run provider detection, menu selection, inline login/Ollama pick/key verification, and build starter models."""
    if not interactive:
        preset = explicit_preset or "openai"
        api_key, key_ready = _prompt_provider_key(
            preset,
            runtime_paths,
            interactive=False,
            for_config_init=for_config_init,
            io=io,
        )
        return default_model_selection(preset, ready=key_ready), api_key

    from mindroom.cli.setup_detection import (  # noqa: PLC0415
        DetectionSnapshot,
        detect_available_providers,
        detect_single_provider,
    )

    if explicit_preset is None:
        with io.console.status("Detecting local logins, API keys, and local servers..."):
            snapshot = detect_available_providers(runtime_paths)
        if summary := snapshot.summary_line():
            io.console.print(f"[dim]{escape(summary)}[/dim]")
        choice = prompt_provider_choice(snapshot, select_fn=io.select)
    else:
        single = detect_single_provider(explicit_preset, runtime_paths)
        snapshot = DetectionSnapshot(providers={explicit_preset: single} if single is not None else {})
        choice = explicit_preset

    return _finalize_interactive_choice(choice, snapshot, runtime_paths, for_config_init=for_config_init, io=io)


def _finalize_interactive_choice(
    choice: ProviderMenuChoice,
    snapshot: DetectionSnapshot,
    runtime_paths: RuntimePaths,
    *,
    for_config_init: bool,
    io: WizardIO,
) -> tuple[StarterModelSelection, str | None]:
    """Collect provider-specific follow-up answers (model pick, inline CLI login, or verified API key)."""
    if choice == "claude_code+codex":
        sonnet_label = CONFIG_INIT_MODEL_PRESETS["claude_code"].display_name or "Claude Sonnet 5.5"
        opus_label = dict(CONFIG_INIT_ADDITIONAL_MODELS["claude_code"])["opus"].display_name or "Claude Opus 5.5"
        default_choice: CombinedDefaultChoice = io.select(
            "Default model for Mind?",
            [("sonnet", sonnet_label), ("sol", "Codex Sol"), ("opus", opus_label)],
        )
        claude_det = snapshot.get("claude_code")
        return (
            _combined_claude_codex_selection(
                default_choice,
                claude_cli_path=claude_det.cli_path if claude_det else None,
                ready=snapshot.both_claude_and_codex_ready(),
            ),
            None,
        )

    if choice in {"codex", "claude_code"}:
        detected = _maybe_run_inline_cli_login(choice, snapshot.get(choice), runtime_paths, io=io)
        claude_cli_path = detected.cli_path if choice == "claude_code" and detected else None
        return (
            default_model_selection(choice, claude_cli_path=claude_cli_path, ready=bool(detected and detected.ready)),
            None,
        )

    if choice == "ollama":
        detected = snapshot.get("ollama")
        if detected and detected.ready and detected.ollama_models:
            width = max(len(model.name) for model in detected.ollama_models) + 2
            chosen_model = io.select(
                "Default local model?",
                [
                    (model.name, f"{model.name:<{width}}{size}" if (size := model.format_size()) else model.name)
                    for model in detected.ollama_models
                ],
            )
            return _ollama_model_selection(chosen_model, detected.ollama_models), None
        return default_model_selection("ollama", ready=False), None

    if choice == "kimi":
        detected = snapshot.get("kimi")
        return default_model_selection("kimi", ready=bool(detected and detected.ready)), None

    api_key, key_ready = _prompt_provider_key(
        choice,
        runtime_paths,
        interactive=True,
        for_config_init=for_config_init,
        io=io,
    )
    return default_model_selection(choice, ready=key_ready), api_key
