"""Real storage layouts in which sandboxed code replaced an ancestor directory with a link.

Workers mount agent state roots and private-instance roots writable, so code in a
worker can swap any directory below its mount for a link to another tenant's
files after the primary resolved its paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_resolution import ResolvedAgentRuntime, resolve_agent_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

PRIVATE_AGENT = "general"
SHARED_AGENT = "helper"
VICTIM_NOTE = "victim-only note"
# Linux reports a link met by a no-follow walk as ELOOP, or ENOTDIR for a directory walk.
LINK_REFUSED = r"Too many levels|Not a directory"
SwappedAncestor = Literal["agent", "workspace"]


def identity(requester: str) -> ToolExecutionIdentity:
    """Return one requester's execution identity for the private agent."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=PRIVATE_AGENT,
        requester_id=f"@{requester}:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id=f"session-{requester}",
    )


ATTACKER = identity("mallory")
VICTIM = identity("alice")


def swap_config(storage: Path, **agent_fields: object) -> Config:
    """Bind a config with one file-memory private agent and one file-memory shared agent."""
    runtime_paths = resolve_runtime_paths(
        config_path=storage / "config.yaml",
        storage_path=storage,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    return bind_runtime_paths(
        Config(
            agents={
                PRIVATE_AGENT: AgentConfig(
                    display_name="General",
                    memory_backend="file",
                    private=AgentPrivateConfig(per="user_agent", root="workspace/mind_data"),
                    **agent_fields,
                ),
                SHARED_AGENT: AgentConfig(display_name="Helper", memory_backend="file"),
            },
        ),
        runtime_paths,
    )


def replace_with_link(directory: Path, target: Path) -> None:
    """Swap a directory for a link to ``target``, keeping the original contents aside."""
    directory.rename(directory.with_name(f"{directory.name}-moved"))
    directory.symlink_to(target, target_is_directory=True)


@dataclass(frozen=True)
class PrivateLayout:
    """Resolved runtimes of an attacker and a victim instance of one private agent."""

    config: Config
    runtime_paths: RuntimePaths
    attacker: ResolvedAgentRuntime
    victim: ResolvedAgentRuntime

    @property
    def victim_workspace(self) -> Path:
        """Return the victim's private workspace root."""
        assert self.victim.workspace is not None
        return self.victim.workspace.root

    @property
    def attacker_workspace(self) -> Path:
        """Return the attacker's private workspace root, as resolved before the swap."""
        assert self.attacker.workspace is not None
        return self.attacker.workspace.root

    def swap(self, ancestor: SwappedAncestor) -> None:
        """Point the attacker's agent or workspace directory at the victim's."""
        if ancestor == "agent":
            replace_with_link(self.attacker.state_root, self.victim.state_root)
        else:
            replace_with_link(self.attacker.state_root / "workspace", self.victim.state_root / "workspace")

    def victim_files(self) -> dict[str, bytes]:
        """Return every file below the victim's workspace with its bytes."""
        return {
            path.relative_to(self.victim_workspace).as_posix(): path.read_bytes()
            for path in sorted(self.victim_workspace.rglob("*"))
            if path.is_file()
        }


def private_layout(storage: Path, *, victim_files: dict[str, str], **agent_fields: object) -> PrivateLayout:
    """Build both private instances and seed the victim's workspace files."""
    config = swap_config(storage, **agent_fields)
    runtime_paths = runtime_paths_for(config)
    runtimes = [
        resolve_agent_runtime(PRIVATE_AGENT, config, runtime_paths, execution_identity=requester, create=True)
        for requester in (ATTACKER, VICTIM)
    ]
    layout = PrivateLayout(config=config, runtime_paths=runtime_paths, attacker=runtimes[0], victim=runtimes[1])
    for relative_path, text in victim_files.items():
        victim_path = layout.victim_workspace / relative_path
        victim_path.parent.mkdir(parents=True, exist_ok=True)
        victim_path.write_text(text, encoding="utf-8")
    return layout
