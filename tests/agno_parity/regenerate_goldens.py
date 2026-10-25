"""Rewrite every golden that owned code produces, from the scenarios and inputs the goldens hold.

Run `.venv/bin/python -m tests.agno_parity.regenerate_goldens` from the repository root, with every extra installed,
after a deliberate change to what a golden pins, then review the rewritten files under `tests/fixtures/agno_parity/`
and commit them with the change; the command lists every file it rewrote.
Each family is regenerated through the support functions its parity tests compare with, so the files hold exactly
what those tests expect, and an unchanged golden is reproduced byte for byte:

- `providers/`: each golden's input is sent through the owned adapter and the production pipeline stages, answered
  with its canned reply, and the request and parsed reply are rewritten (`tests/providers/golden_support.py`).
- `prompts/`: each scenario of `tests/agno_parity/prompt_scenarios.py` runs again against the recording provider.
- `tools/`: every registered tool is built as `tests/tool_system/test_agno_toolkits.py` builds it, and every case
  already in `arguments.json` runs as `tests/tool_system/test_tool_invocation.py` runs it; a new argument case needs
  only its `function` and `raw_arguments` before a run fills in the rest.
- `knowledge/`: every embedder call and every search runs again (`tests/agno_parity/knowledge_goldens.py`);
  `rows.json` holds the rows Agno wrote, a read-only input like the storage, run, history, and codec goldens.
- `events/`: the OpenAI-compatible API requests run again (`tests/agno_parity/openai_compat_golden.py`), and every
  run of `ai_run.json` replays or resumes through the entry point that published its metadata
  (`tests/ai_run_golden_helpers.py`).

Nothing reaches a provider or any other service: replies come from the goldens' canned responses and scripted models,
provider credentials and endpoints are hidden from the environment, and every network connection is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import tempfile
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import structlog

from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests import ai_run_golden_helpers as ai_run
from tests.agno_parity import knowledge_goldens as knowledge
from tests.agno_parity import openai_compat_golden, prompt_scenarios
from tests.agno_parity.golden_files import sorted_keys, write
from tests.providers import golden_support
from tests.providers.anthropic_family_support import FAMILIES as CLAUDE_FAMILIES
from tests.providers.anthropic_family_support import run_golden
from tests.providers.gemini_ollama_support import FAMILIES as GEMINI_OLLAMA_FAMILIES
from tests.providers.gemini_ollama_support import Golden, replay
from tests.providers.openai_family_support import OPENAI_FAMILIES, replay_golden
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.providers.golden_support import Replay


# Each family's regenerated goldens, keyed by the file each one is written to.
type Goldens = dict[Path, object]


def _refuse_network(*_args: object, **_kwargs: object) -> None:
    msg = "Golden regeneration refuses network connections"
    raise OSError(msg)


@contextmanager
def offline() -> Iterator[None]:
    """Refuse every connection, and keep Google's SDKs from probing for cloud credentials or a metadata server."""
    with (
        tempfile.TemporaryDirectory(prefix="golden-gcloud-") as gcloud_config,
        pytest.MonkeyPatch.context() as monkeypatch,
        patch.object(socket.socket, "connect", _refuse_network),
        patch.object(socket.socket, "connect_ex", _refuse_network),
        patch.object(socket, "getaddrinfo", _refuse_network),
        patch.object(socket, "gethostbyname", _refuse_network),
        patch.object(socket, "gethostbyname_ex", _refuse_network),
    ):
        monkeypatch.setenv("CLOUDSDK_CONFIG", gcloud_config)
        monkeypatch.setenv("NO_GCE_CHECK", "true")
        yield


async def _replay_provider_golden(golden: dict[str, Any], root: Path) -> Replay:
    family = golden["family"]
    if family in CLAUDE_FAMILIES:
        return await run_golden(golden, root)
    if family in OPENAI_FAMILIES:
        return await replay_golden(golden, root)
    return await replay(Golden(family, golden["scenario"], golden), root)


async def regenerated_provider_golden(path: Path) -> dict[str, Any]:
    """Replay the provider golden at ``path`` through its family's owned adapter and return the golden it regenerates.

    The caller hides the provider environment, as ``hide_provider_environment`` does.
    """
    golden = golden_support.load_golden(path)
    with tempfile.TemporaryDirectory(prefix="provider-golden-") as root:
        return golden_support.regenerated_golden(golden, await _replay_provider_golden(golden, Path(root)))


def provider_goldens() -> Goldens:
    """Replay every provider golden through its owned adapter, for its request and parsed reply."""
    families = (*CLAUDE_FAMILIES, *OPENAI_FAMILIES, *GEMINI_OLLAMA_FAMILIES)
    stored = {path.name for path in golden_support.FIXTURE_ROOT.iterdir() if path.is_dir()}
    assert stored == set(families), f"provider golden families without a replay: {sorted(stored - set(families))}"
    with pytest.MonkeyPatch.context() as monkeypatch:
        golden_support.hide_provider_environment(monkeypatch)
        return {path: asyncio.run(regenerated_provider_golden(path)) for path in golden_support.golden_paths(families)}


async def regenerated_prompt_golden(name: str) -> dict[str, Any]:
    """Run the prompt scenario ``name`` against the recording provider and return the golden it regenerates."""
    with tempfile.TemporaryDirectory(prefix="prompt-golden-") as root:
        recorded = await prompt_scenarios.record_scenario(name, Path(root))
        return prompt_scenarios.regenerated_golden(name, recorded, Path(root))


def prompt_goldens() -> Goldens:
    """Run every prompt scenario against the recording provider."""
    return {
        prompt_scenarios.golden_path(name): asyncio.run(regenerated_prompt_golden(name))
        for name in sorted(prompt_scenarios.SCENARIOS)
    }


def _tool_schemas(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Build every registered tool with its default configuration and, when it has switches, with all of them on."""
    schemas: dict[str, object] = {}
    all_enabled: dict[str, object] = {}
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
        for tool_name in sorted(BUILTIN_TOOL_METADATA):
            if tool_name in support.UNBUILDABLE:
                continue
            if (missing := support.missing_dependencies(tool_name)) is not None:
                msg = f"{tool_name} needs {missing}; the goldens cover every tool, so sync every extra first"
                raise SystemExit(msg)
            toolkit = support.build_toolkit(tool_name, config, runtime_paths)
            schemas[tool_name] = support.golden_entry(adapt_toolkit(toolkit))
            if overrides := support.all_enabled_overrides(tool_name):
                toolkit = support.build_toolkit(tool_name, config, runtime_paths, overrides)
                all_enabled[tool_name] = {
                    "overrides": overrides,
                    **support.golden_entry(adapt_toolkit(toolkit)),
                }
    return schemas, all_enabled


async def argument_golden() -> dict[str, dict[str, object]]:
    """Run every case of the committed argument golden again and return the golden, its keys sorted."""
    cases = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))
    functions = support.probe_functions()
    return sorted_keys({case_id: await support.argument_outcome(case, functions) for case_id, case in cases.items()})


def tool_goldens() -> Goldens:
    """Build the three tool goldens: schemas keep the key order providers receive, arguments are sorted."""
    with tempfile.TemporaryDirectory(prefix="tool-golden-") as root:
        schemas, all_enabled = _tool_schemas(Path(root).resolve())
    return {
        support.SCHEMAS_PATH: schemas,
        support.ALL_ENABLED_SCHEMAS_PATH: all_enabled,
        support.ARGUMENTS_PATH: asyncio.run(argument_golden()),
    }


def knowledge_goldens() -> Goldens:
    """Run every embedder case and search again; ``rows.json`` holds the rows Agno wrote and is never rewritten."""
    embedders: dict[str, object] = {}
    for name, case in knowledge.load(knowledge.EMBEDDERS_PATH).items():
        with tempfile.TemporaryDirectory(prefix="knowledge-golden-") as root, pytest.MonkeyPatch.context() as patcher:
            embedders[name] = knowledge.embedder_case(case, Path(root), patcher)
    with tempfile.TemporaryDirectory(prefix="knowledge-golden-") as root, pytest.MonkeyPatch.context() as patcher:
        search = asyncio.run(knowledge.regenerated_search(Path(root), knowledge.install_parity_embedder(patcher)))
    return {knowledge.EMBEDDERS_PATH: embedders, knowledge.SEARCH_PATH: search}


async def _published_run_metadata(run_id: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="event-golden-") as root, pytest.MonkeyPatch.context() as patcher:
        return await ai_run.published_metadata(run_id, Path(root), patcher)


def event_goldens() -> Goldens:
    """Send the OpenAI-compatible API requests again and publish every golden run's ``io.mindroom.ai_run`` metadata."""
    with tempfile.TemporaryDirectory(prefix="event-golden-") as root:
        compat = openai_compat_golden.regenerated_golden(Path(root))
    published = {run_id: asyncio.run(_published_run_metadata(run_id)) for run_id in ai_run.GOLDEN_RUN_METADATA}
    return {openai_compat_golden.GOLDEN: compat, ai_run.GOLDEN: ai_run.regenerated_golden(published)}


def regenerate() -> list[Path]:
    """Rewrite every regenerable golden family and return the files whose text changed.

    Every family is regenerated before any file is written, so a family that fails leaves every golden as it was.
    """
    families = (provider_goldens, prompt_goldens, tool_goldens, knowledge_goldens, event_goldens)
    goldens = [family() for family in families]
    return [path for regenerated in goldens for path, golden in regenerated.items() if write(path, golden)]


def main() -> None:
    """Rewrite the goldens offline and list the files that changed."""
    # Tool construction and every request log and warn incidentally; quiet them so the list of rewritten files stands
    # out. A few SDKs print directly while they import, which nothing here silences.
    structlog.configure(processors=[], logger_factory=structlog.ReturnLoggerFactory())
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    with offline():
        changed = regenerate()
    root = Path.cwd()
    for path in changed:
        print(f"rewrote {path.relative_to(root) if path.is_relative_to(root) else path}")
    print(f"{len(changed)} golden files changed")


if __name__ == "__main__":
    main()
