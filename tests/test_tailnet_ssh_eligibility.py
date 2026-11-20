"""Tests for the tailnet SSH config surface and the one eligibility rule its link, launch, and API paths share."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.config.tailnet_ssh import normalize_public_key
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tailnet_ssh.eligibility import Ineligible, link_ineligibility, ssh_eligible_launch
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    subagent_worker_key,
    user_agent_worker_key_requester,
)
from mindroom.workers.backends._dedicated_worker_common import SharedDirectoryMount
from mindroom.workers.models import WorkerLaunchConfig

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
_ALICE_MINE_KEY = "v1:default:user_agent:~@alice:example.org:mine"

# The suite's autouse bypass would admit everyone; this rule is about who is admitted.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _paths(tmp_path: Path, *, backend: str | None = "docker") -> RuntimePaths:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    env = {"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""}
    if backend is not None:
        env["MINDROOM_WORKER_BACKEND"] = backend
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "mindroom_data", process_env=env)


def _mine_like(**overrides: object) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "display_name": "Mine",
        "worker_scope": "user_agent",
        "access": {"users": [ALICE], "members_of_rooms": []},
        "tailnet_ssh": True,
    }
    return agent | overrides


_DATA: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
    "rooms": {"lobby": {}},
    "agents": {
        "mind": {
            "display_name": "Mind",
            "private": {"per": "user_agent"},
            "tailnet_ssh": True,
            "access": {"users": ["@*:example.org"]},
        },
        "mine": _mine_like(),
        "coowned": _mine_like(access={"users": [ALICE, BOB], "members_of_rooms": []}),
        # Load refuses tailnet_ssh with a room grant, so `_matrix_config` turns it on after load.
        "roomy": _mine_like(access={"users": [ALICE], "members_of_rooms": ["lobby"]}, tailnet_ssh=False),
        "plain": {k: v for k, v in _mine_like().items() if k != "tailnet_ssh"},
        "other": {"display_name": "Other", "worker_scope": "user_agent"},
    },
}


def _data() -> dict[str, Any]:
    return deepcopy(_DATA)


def _load(paths: RuntimePaths, data: dict[str, Any] | None = None) -> Config:
    return Config.validate_with_runtime(_data() if data is None else data, paths)


def _matrix_config(paths: RuntimePaths) -> Config:
    """The builder's config with roomy opted in too, as a link or launch must still refuse it."""
    config = _load(paths)
    config.agents["roomy"].tailnet_ssh = True
    return config


def _key(requester: str, agent: str, scope: str = "user_agent") -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id=requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    key = resolve_worker_key(scope, identity)  # type: ignore[arg-type]
    assert key is not None
    return key


def _ineligibility(
    paths: RuntimePaths,
    config: Config,
    requester: str,
    agent: str,
    worker_key: str | None = None,
) -> Ineligible | None:
    key = _key(requester, agent) if worker_key is None else worker_key
    return link_ineligibility(config, paths, requester, agent, key, AgentReplyMembershipIndex())


def _public_key() -> str:
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
    )
    return raw.decode()


@pytest.mark.parametrize(
    ("agent", "requester", "reason"),
    [
        ("mind", ALICE, None),
        ("mine", ALICE, None),
        ("plain", ALICE, "not_enabled"),
        ("coowned", ALICE, "shared_workspace"),
        ("roomy", ALICE, "shared_workspace"),
        ("mine", BOB, "not_authorized"),
    ],
)
def test_link_ineligibility_matrix(tmp_path: Path, agent: str, requester: str, reason: str | None) -> None:
    """Only a private agent or one whose workspace is the requester's alone is eligible, and only for its user."""
    paths = _paths(tmp_path)
    result = _ineligibility(paths, _matrix_config(paths), requester, agent)
    assert (None if result is None else result.reason) == reason


def test_alice_key_for_mine_is_the_documented_shape(tmp_path: Path) -> None:
    """The key a link stores is the one worker routing resolves for the requester and agent."""
    assert _key(ALICE, "mine") == _ALICE_MINE_KEY
    paths = _paths(tmp_path)
    assert _ineligibility(paths, _load(paths), BOB, "mine", _ALICE_MINE_KEY) == Ineligible("not_authorized")


def test_another_persons_key_is_not_authorized(tmp_path: Path) -> None:
    """A requester the agent admits still cannot link to a worker key that names someone else."""
    paths = _paths(tmp_path)
    assert _ineligibility(paths, _load(paths), ALICE, "mind", _key(BOB, "mind")) == Ineligible("not_authorized")


def test_static_runner_backend_is_backend(tmp_path: Path) -> None:
    """Without a dedicated backend nothing is eligible, and that is reported before any other reason."""
    paths = _paths(tmp_path, backend=None)
    config = _load(paths)
    assert _ineligibility(paths, config, ALICE, "mine") == Ineligible("backend")
    assert _ineligibility(paths, config, BOB, "plain") == Ineligible("backend")


def test_subagent_pinned_key_is_scope(tmp_path: Path) -> None:
    """A subagent's pinned worker is not the person's own worker."""
    paths = _paths(tmp_path)
    pinned = subagent_worker_key(_ALICE_MINE_KEY, "a" * 32)
    assert _ineligibility(paths, _load(paths), ALICE, "mine", pinned) == Ineligible("scope")


def test_scope_refusals(tmp_path: Path) -> None:
    """A missing key, a key of another scope, and a key for another agent are all scope failures."""
    paths = _paths(tmp_path)
    config = _load(paths)
    membership = AgentReplyMembershipIndex()
    assert link_ineligibility(config, paths, ALICE, "mine", None, membership) == Ineligible("scope")
    assert _ineligibility(paths, config, ALICE, "mine", _key(ALICE, "mine", "user")) == Ineligible("scope")
    assert _ineligibility(paths, config, ALICE, "mine", _key(ALICE, "other")) == Ineligible("scope")


def test_joined_directory_names_it(tmp_path: Path) -> None:
    """A directory the agent joined is another agent's data in its worker, so the card names it."""
    paths = _paths(tmp_path)
    config = _load(paths)
    create_shared_directory(paths, owner="other", name="specs")
    add_shared_directory_member(paths, owner="other", name="specs", member="mine")
    assert _ineligibility(paths, config, ALICE, "mine") == Ineligible("joined_directory", "other/specs")


def test_owned_directory_and_knowledge_base_pass(tmp_path: Path) -> None:
    """Only the agent writes a directory it owns, and an assigned knowledge base is mounted read-only."""
    paths = _paths(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    data = _data()
    data["knowledge_bases"] = {"docs": {"path": str(docs)}}
    data["agents"]["mine"]["knowledge_bases"] = ["docs"]
    config = _load(paths, data)
    create_shared_directory(paths, owner="mine", name="notes")
    assert _ineligibility(paths, config, ALICE, "mine") is None


@pytest.mark.parametrize("scope", ["shared", "user", None])
def test_config_error_names_agent_and_scope(tmp_path: Path, scope: str | None) -> None:
    """tailnet_ssh on an agent whose worker is not per person and agent fails the load and says why."""
    data = _data()
    data["agents"]["x"] = (
        _mine_like(worker_scope=scope) if scope else {k: v for k, v in _mine_like().items() if k != "worker_scope"}
    )
    with pytest.raises(ValueError, match=r"agents\.x\.tailnet_ssh requires ") as raised:
        _load(_paths(tmp_path), data)
    assert "user_agent" in str(raised.value)


@pytest.mark.parametrize(
    "access",
    [
        {"users": ["@*:example.org"], "members_of_rooms": []},
        {"users": [ALICE], "members_of_rooms": ["lobby"]},
        {"users": [ALICE], "members_of_rooms": [], "current_room_members": True},
        None,
    ],
)
def test_config_error_requires_private_or_concrete_users(tmp_path: Path, access: dict[str, Any] | None) -> None:
    """A shared agent needs access limited to concrete users with no room grants."""
    data = _data()
    data["agents"]["x"] = _mine_like(access=access)
    with pytest.raises(ValueError, match=r"agents\.x\.tailnet_ssh requires .*access\.users"):
        _load(_paths(tmp_path), data)


def test_config_load_accepts_two_users(tmp_path: Path) -> None:
    """A co-owner added later suspends SSH; it must not make the config unloadable."""
    config = _load(_paths(tmp_path))
    assert config.agents["coowned"].tailnet_ssh is True
    assert config.agents["other"].tailnet_ssh is False


def test_personal_agent_template_accepts_tailnet_ssh(tmp_path: Path) -> None:
    """A personal agent serves only its owner, so its template may turn tailnet SSH on."""
    data = _data()
    data["personal_agents"] = {
        "pa": {"agent": {"display_name": "PA", "worker_scope": "user_agent", "tailnet_ssh": True}},
    }
    config = _load(_paths(tmp_path), data)
    assert config.personal_agents["pa"].agent.tailnet_ssh is True

    data["personal_agents"]["pa"]["agent"]["worker_scope"] = "user"
    with pytest.raises(ValueError, match=r"tailnet_ssh requires "):
        _load(_paths(tmp_path / "user"), data)


@pytest.mark.parametrize("requester", ["@alice:example.org", "@Bas.N:example.org:8448", "@a%b:example.org"])
def test_user_agent_worker_key_requester_round_trips_odd_ids(requester: str) -> None:
    """The requester is read back from a user_agent key however its ID is encoded."""
    assert user_agent_worker_key_requester(_key(requester, "mine")) == requester


def test_user_agent_worker_key_requester_refuses_other_keys() -> None:
    """Only an unpinned user_agent key names one requester's own worker."""
    assert user_agent_worker_key_requester(_key(ALICE, "mine", "user")) is None
    assert user_agent_worker_key_requester(_key(ALICE, "mine", "shared")) is None
    assert user_agent_worker_key_requester(resolve_unscoped_worker_key("mine")) is None
    assert user_agent_worker_key_requester(subagent_worker_key(_ALICE_MINE_KEY, "a" * 32)) is None
    assert user_agent_worker_key_requester(script_worker_key_for_run(_ALICE_MINE_KEY, "script-" + "a" * 32)) is None
    # A requester part spelled differently from how worker routing encodes it is not the same key.
    assert user_agent_worker_key_requester("v1:default:user_agent:~@alice%3Aexample.org:mine") is None
    assert user_agent_worker_key_requester("v1:default:user_agent:@alice:example.org:mine") is None


def test_normalize_public_key_shapes() -> None:
    """One public key line is kept; options, private keys, empty lines, and junk are refused with a sentence."""
    key = _public_key()
    assert normalize_public_key(f"  {key} me@host\r\n") == f"{key} me@host"
    assert normalize_public_key(key) == key
    with pytest.raises(ValueError, match="option"):
        normalize_public_key(f'command="x" {key}')
    with pytest.raises(ValueError, match="private key"):
        normalize_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key("")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key("ssh-ed25519 AAAAnotakey me@host")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key(f'{key} me\ncommand="x" {key}')


def test_config_authorized_keys_are_normalized_and_keyed_by_concrete_ids(tmp_path: Path) -> None:
    """Operator keys are stored normalized, and every person is named by a concrete Matrix ID."""
    key = _public_key()
    data = _data()
    data["tailnet_ssh"] = {"logins": {ALICE: "alice@github"}, "authorized_keys": {ALICE: [f"{key} me@host\r\n"]}}
    config = _load(_paths(tmp_path), data)
    assert config.tailnet_ssh.authorized_keys == {ALICE: [f"{key} me@host"]}
    assert config.tailnet_ssh.logins == {ALICE: "alice@github"}

    for section in (
        {"authorized_keys": {"@*:example.org": [key]}},
        {"logins": {"@*:example.org": "everyone@github"}},
        {"authorized_keys": {ALICE: ["-----BEGIN OPENSSH PRIVATE KEY-----"]}},
        {"unknown": {}},
    ):
        data["tailnet_ssh"] = section
        with pytest.raises(ValueError, match="tailnet_ssh"):
            _load(_paths(tmp_path), data)


def test_empty_tailnet_ssh_section_is_left_out_of_authored_config(tmp_path: Path) -> None:
    """A null section loads empty and is not written back."""
    data = _data()
    data["tailnet_ssh"] = None
    config = _load(_paths(tmp_path), data)
    assert config.tailnet_ssh.logins == {}
    assert "tailnet_ssh" not in config.authored_model_dump()


def _launch(config: Config) -> WorkerLaunchConfig:
    return WorkerLaunchConfig(
        config=config,
        config_data={},
        agent_policies=config.get_agent_policies(),
        tool_validation_snapshot={},
        grantable_credentials=frozenset(),
        digest="digest",
    )


def test_ssh_eligible_launch_matrix(tmp_path: Path) -> None:
    """A launch is marked only for an opted-in per person and agent worker whose workspace is that person's alone."""
    launch = _launch(_matrix_config(_paths(tmp_path)))
    mind_key = _key(ALICE, "mind")
    owned = SharedDirectoryMount(Path("/data/notes"), Path("/app/shared/mine/notes"), read_only=False)
    joined = SharedDirectoryMount(Path("/data/specs"), Path("/app/shared/other/specs"), read_only=True)

    assert ssh_eligible_launch(mind_key, launch, frozenset({"mind"}), ()) is True
    assert ssh_eligible_launch(_ALICE_MINE_KEY, launch, None, ()) is True
    assert ssh_eligible_launch(_ALICE_MINE_KEY, launch, None, (owned,)) is True

    assert ssh_eligible_launch(_key(ALICE, "plain"), launch, None, ()) is False
    assert ssh_eligible_launch(_key(ALICE, "coowned"), launch, None, ()) is False
    assert ssh_eligible_launch(_key(ALICE, "roomy"), launch, None, ()) is False
    assert ssh_eligible_launch(_key(BOB, "mine"), launch, None, ()) is False
    assert ssh_eligible_launch(_key(ALICE, "mine", "user"), launch, None, ()) is False
    assert ssh_eligible_launch(subagent_worker_key(_ALICE_MINE_KEY, "a" * 32), launch, None, ()) is False
    assert (
        ssh_eligible_launch(script_worker_key_for_run(_ALICE_MINE_KEY, "script-" + "a" * 32), launch, None, ()) is False
    )
    assert ssh_eligible_launch(mind_key, launch, None, ()) is False
    assert ssh_eligible_launch(mind_key, launch, frozenset({"other"}), ()) is False
    assert ssh_eligible_launch(_ALICE_MINE_KEY, launch, None, (owned, joined)) is False
    assert ssh_eligible_launch(mind_key, launch, frozenset({"mind"}), (joined,)) is False


def test_ssh_eligible_launch_refuses_an_ambiguous_agent_part(tmp_path: Path) -> None:
    """A key's agent part must name exactly one agent of the launch."""
    data = _data()
    data["agents"]["my_desk"] = _mine_like()
    launch = _launch(_load(_paths(tmp_path), data))
    policies = dict(launch.agent_policies)
    # Configured names cannot collide this way; a launch must still refuse to guess.
    policies["my desk"] = policies["my_desk"]
    ambiguous = WorkerLaunchConfig(
        config=launch.config,
        config_data={},
        agent_policies=policies,
        tool_validation_snapshot={},
        grantable_credentials=frozenset(),
        digest="digest",
    )
    key = _key(ALICE, "my_desk")
    assert ssh_eligible_launch(key, launch, None, ()) is True
    assert ssh_eligible_launch(key, ambiguous, None, ()) is False
    assert ssh_eligible_launch(_key(ALICE, "nobody"), launch, None, ()) is False
