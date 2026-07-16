"""Agent rescue docs seeded into the config directory by `mindroom config init`.

The config directory gets an AGENTS.md (plus a CLAUDE.md symlink) so that when an
installation breaks — typically after an upgrade with an incompatible config — the
user can point any coding agent at the directory and it knows what MindRoom is,
where the docs are, and how to diagnose and fix the problem.
"""

from __future__ import annotations

from pathlib import Path

_AGENT_DOCS_TEMPLATE = """\
# MindRoom Configuration

This directory holds the configuration for a MindRoom installation.
MindRoom is an open-source AI agent platform: agents live in Matrix chat rooms and are configured entirely from this directory.

- Source: https://github.com/mindroom-ai/mindroom
- Documentation: https://docs.mindroom.chat/
- LLM-readable docs index: https://raw.githubusercontent.com/mindroom-ai/mindroom/refs/heads/main/skills/mindroom-docs/references/llms.txt
- Complete docs in one file (~1 MB): https://raw.githubusercontent.com/mindroom-ai/mindroom/refs/heads/main/skills/mindroom-docs/references/llms-full.txt

If you are a coding agent asked to repair this installation (for example, MindRoom does not start after an upgrade), work from this directory using the notes below.

## What lives where

- `{config_file}` — the main configuration: models, agents, teams, rooms, authorization. It can pull in other YAML files with `!include`.
- `.env` — environment variables and secrets (API keys, Matrix settings). Sensitive: never publish or commit its contents.
- Runtime state (logs, sessions, credentials, Matrix encryption keys) lives in the storage root: the `MINDROOM_STORAGE_PATH` value in `.env` (initialized to `{storage_root}`).

## How to diagnose

Use `mindroom <command>` if MindRoom is installed, otherwise `uvx mindroom <command>`.

1. `mindroom config validate` — parses the config and lists exactly which keys are invalid. After a failed upgrade, start here: the usual cause is a config schema change.
2. `mindroom doctor` — preflight checks for Matrix connectivity, credentials, and config.
3. Read the newest `mindroom_*.log` under `logs/` in the storage root for the actual startup error.
4. `mindroom config path` — confirm this is the config file MindRoom actually loads.
5. `mindroom run --log-level DEBUG` — reproduce with verbose logging.

## How to fix

- Schema errors: make the smallest edit to `{config_file}` that makes `mindroom config validate` pass; the documentation above describes every field.
- Missing API keys: add them to `.env` (`mindroom config validate` warns about missing provider keys).
- Roll back an upgrade by pinning the previous version: `uvx mindroom@<version> run`.
- Last resort: `mindroom config init --force` regenerates a starter config — this discards customizations, so copy `{config_file}` aside first.

## Cautions

- Do not delete `encryption_keys/` or `matrix_state.yaml` from the storage root; that breaks Matrix sessions and end-to-end encryption.
- Keep `.env` and the storage root's `credentials/` out of version control and out of anything you publish.
"""

_AGENTS_DOC_NAME = "AGENTS.md"
_CLAUDE_DOC_NAME = "CLAUDE.md"


def _render_config_agent_docs(*, config_path: Path, storage_root: Path) -> str:
    """Render the AGENTS.md rescue guide for one config directory."""
    return _AGENT_DOCS_TEMPLATE.format(
        config_file=config_path.name,
        storage_root=storage_root.expanduser().resolve(),
    )


def ensure_config_agent_docs(
    config_dir: Path,
    *,
    config_path: Path,
    storage_root: Path,
    force: bool = False,
) -> list[Path]:
    """Seed AGENTS.md and a CLAUDE.md symlink next to the config file.

    Existing files are left untouched unless ``force`` is set. Returns the paths
    that were created or replaced.
    """
    content = _render_config_agent_docs(config_path=config_path, storage_root=storage_root)
    created: list[Path] = []

    agents_path = config_dir / _AGENTS_DOC_NAME
    if force or not (agents_path.is_symlink() or agents_path.exists()):
        # Unlink first so --force replaces a symlinked AGENTS.md instead of
        # writing through it into the symlink target.
        agents_path.unlink(missing_ok=True)
        agents_path.write_text(content, encoding="utf-8")
        created.append(agents_path)

    claude_path = config_dir / _CLAUDE_DOC_NAME
    if claude_path.is_symlink() and claude_path.readlink() == Path(_AGENTS_DOC_NAME):
        return created
    if (claude_path.is_symlink() or claude_path.exists()) and not force:
        return created

    claude_path.unlink(missing_ok=True)
    try:
        claude_path.symlink_to(_AGENTS_DOC_NAME)
    except OSError:
        # Symlinks can be unavailable (e.g. Windows without developer mode);
        # a plain copy of AGENTS.md — which may hold preserved user content —
        # gives coding agents the same entry point.
        claude_path.write_text(agents_path.read_text(encoding="utf-8"), encoding="utf-8")
    created.append(claude_path)
    return created
