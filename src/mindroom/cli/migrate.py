"""Configuration migration CLI command."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer

from mindroom import constants
from mindroom.cli.config import CONFIG_PATH_OPTION, console, format_validation_errors, load_config_quiet

_OLD_CONFIG_INIT_MIND_MEMORY_TOOL_BLOCK = """\
    knowledge_bases:
      - mind_memory
    tools:
      - shell
      - coding
"""
_CONFIG_INIT_MIND_MEMORY_TOOL_BLOCK = """\
    tools:
      - shell
      - coding
      - memory
"""
_OLD_CONFIG_INIT_MIND_MEMORY_INSTRUCTION = (
    "      - Before answering prior-history questions, search memory files first when a knowledge base is configured."
)
_CONFIG_INIT_MIND_MEMORY_INSTRUCTION = "      - Before answering prior-history questions, use search_memories first."
_OLD_CONFIG_INIT_MIND_MEMORY_PATHS = (
    "${MINDROOM_STORAGE_PATH}/agents/mind/workspace/memory",
    "./mindroom_data/agents/mind/workspace/memory",
)
_OLD_CONFIG_INIT_MEMORY_SEARCH_INSERTION_POINT = """\
  file:
    max_entrypoint_lines: 200
  auto_flush:
"""
_CONFIG_INIT_MEMORY_SEARCH_BLOCK = """\
  file:
    max_entrypoint_lines: 200
  search:
    mode: semantic
    include:
      - memory/**/*.md
    include_entrypoint: false
  auto_flush:
"""


def _resolve_config_path(path: Path | None) -> Path:
    """Resolve the config file path from explicit argument or default."""
    if path is not None:
        return path.expanduser().resolve()
    return constants.resolve_primary_runtime_paths(process_env=constants.exported_process_env()).config_path.resolve()


def _old_config_init_mind_memory_knowledge_block(path: str) -> str:
    return f"""\
knowledge_bases:
  mind_memory:
    path: {path}
    watch: true

# File-based memory requires no external LLM, and starter configs use a local embedder for knowledge indexing.
memory:
"""


def _migrate_old_config_init_mind_memory(content: str) -> tuple[str, bool]:
    """Migrate the exact old starter mind_memory wiring to memory.search."""
    if content.count("mind_memory") != 2:
        return content, False

    required_blocks = (
        _OLD_CONFIG_INIT_MIND_MEMORY_TOOL_BLOCK,
        _OLD_CONFIG_INIT_MIND_MEMORY_INSTRUCTION,
        _OLD_CONFIG_INIT_MEMORY_SEARCH_INSERTION_POINT,
    )
    if any(block not in content for block in required_blocks):
        return content, False

    old_knowledge_block = next(
        (
            block
            for path in _OLD_CONFIG_INIT_MIND_MEMORY_PATHS
            if (block := _old_config_init_mind_memory_knowledge_block(path)) in content
        ),
        None,
    )
    if old_knowledge_block is None:
        return content, False

    migrated = content.replace(
        _OLD_CONFIG_INIT_MIND_MEMORY_TOOL_BLOCK,
        _CONFIG_INIT_MIND_MEMORY_TOOL_BLOCK,
        1,
    )
    migrated = migrated.replace(
        _OLD_CONFIG_INIT_MIND_MEMORY_INSTRUCTION,
        _CONFIG_INIT_MIND_MEMORY_INSTRUCTION,
        1,
    )
    migrated = migrated.replace(
        old_knowledge_block,
        "# File-based memory requires no external LLM.\nmemory:\n",
        1,
    )
    migrated = migrated.replace(
        _OLD_CONFIG_INIT_MEMORY_SEARCH_INSERTION_POINT,
        _CONFIG_INIT_MEMORY_SEARCH_BLOCK,
        1,
    )
    return migrated, migrated != content


def config_migrate(
    path: Path | None = CONFIG_PATH_OPTION,
) -> None:
    """Apply safe, text-preserving migrations to config.yaml."""
    config_file = _resolve_config_path(path)

    if not config_file.exists():
        console.print(f"[yellow]No config file found at:[/yellow] {config_file}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        raise typer.Exit(1)

    try:
        content = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_file,
        process_env=constants.exported_process_env(),
    )
    from yaml import YAMLError  # noqa: PLC0415

    try:
        load_config_quiet(runtime_paths, tolerate_plugin_load_errors=True)
        access_migrated_content = config_file.read_text(encoding="utf-8")
    except (ValueError, YAMLError, OSError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    migrated_access = access_migrated_content != content
    migrated, migrated_mind_memory = _migrate_old_config_init_mind_memory(access_migrated_content)
    if not migrated_access and not migrated_mind_memory:
        console.print("[green]No migrations applied.[/green]")
        return

    if migrated_mind_memory:
        from mindroom.config.access_migration import write_text_atomic  # noqa: PLC0415

        try:
            write_text_atomic(config_file, migrated)
        except OSError as exc:
            console.print(f"[red]Error:[/red] Could not write migrated configuration to {config_file}: {exc}")
            raise typer.Exit(1) from None

    if migrated_access:
        console.print("[green]Applied migration:[/green] membership access schema")
    if migrated_mind_memory:
        console.print("[green]Applied migration:[/green] starter Mind file-memory semantic search")
    console.print(f"[green]Config updated:[/green] {config_file}")
