"""A private workspace can never be named after primary-owned state beside it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.agent import _RESERVED_PRIVATE_ROOT_FIRST_PARTS, AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools, _profile_dir
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.matrix_rtc import transcript as transcript_module
from mindroom.memory.config import _get_memory_config
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import TableNames

if TYPE_CHECKING:
    from pathlib import Path


def test_primary_state_written_beside_a_private_workspace_is_reserved(tmp_path: Path) -> None:
    """Every entry the primary writes into a private state root is a reserved private.root first part."""
    state_root = tmp_path / "private_instances" / "scope" / "mind"
    state_root.mkdir(parents=True)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    config = Config(
        agents={"mind": AgentConfig(display_name="Mind")},
        knowledge_bases={"notes": KnowledgeBaseConfig(path=str(tmp_path / "notes"), mode="files")},
    )

    with session_storage_preflight(state_root, TableNames("mind"), timeout_seconds=1):
        pass
    create_agent_state_storage("mind", state_root)
    # Retired Agno Learning left its databases here, and file memory without a workspace its files; a worker must never
    # mount either.
    (state_root / "learning").mkdir()
    (state_root / "memory_files").mkdir()
    _get_memory_config(state_root, config, runtime_paths)
    KnowledgeManager("notes", config=config, runtime_paths=runtime_paths, storage_path=state_root)
    browser = BrowserTools(runtime_paths, agent_state_root=state_root)
    _profile_dir(browser._profiles_root, "mindroom")
    browser._publish_browser_artifact(browser._next_output_path("png"), b"capture")

    written = {entry.name for entry in state_root.iterdir()} | {transcript_module._TRANSCRIPT_DIRNAME}
    assert written <= _RESERVED_PRIVATE_ROOT_FIRST_PARTS
