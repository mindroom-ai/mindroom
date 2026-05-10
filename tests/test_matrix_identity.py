"""Tests for unified Matrix ID handling."""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

import pytest
import yaml

from mindroom import constants as constants_mod
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.identity import (
    MatrixID,
    _ThreadStateKey,
    managed_account_key,
    managed_account_user_id,
    parse_current_matrix_user_id,
    parse_historical_matrix_user_id,
    try_parse_historical_matrix_user_id,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix_identifiers import agent_username_localpart
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


def _entity_name(sender_id: str, config: Config, runtime_paths: constants_mod.RuntimePaths) -> str | None:
    return entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(sender_id)


def _is_entity_id(sender_id: str, config: Config, runtime_paths: constants_mod.RuntimePaths) -> bool:
    return _entity_name(sender_id, config, runtime_paths) is not None


def _bind_runtime_paths(config: Config, tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    bound_config = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(bound_config, runtime_paths)
    return bound_config


class TestMatrixID:
    """Test the MatrixID class."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test_parse_valid_matrix_id(self) -> None:
        """Test parsing a valid Matrix ID."""
        mid = MatrixID.parse("@mindroom_calculator:localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    @pytest.mark.parametrize(
        "matrix_id",
        [
            "@alice:example.org",
            "@alice.foo_bar=bot-/+123:example.org:8448",
            "@alice:1.2.3.4",
            "@alice:[1234:5678::abcd]",
            "@alice:[1234:5678::abcd]:5678",
        ],
    )
    def test_parse_valid_matrix_user_id_grammar(self, matrix_id: str) -> None:
        """Current Matrix user IDs should parse when they match the spec grammar."""
        assert MatrixID.parse(matrix_id).full_id == matrix_id
        assert parse_current_matrix_user_id(matrix_id) == matrix_id

    @pytest.mark.parametrize(
        "matrix_id",
        [
            "@Alice:example.org",
            "@a!b:example.org",
            "@alice example:example.org",
            "@alice\tbob:example.org",
            "@alice\nbob:example.org",
            "@álîçé:example.org",
            "@:example.org",
        ],
    )
    def test_parse_accepts_historical_matrix_user_ids(self, matrix_id: str) -> None:
        """Matrix-originated historical user IDs should remain parseable."""
        assert MatrixID.parse(matrix_id).full_id == matrix_id

    @pytest.mark.parametrize(
        "matrix_id",
        [
            "@Alice:example.org",
            "@a!b:example.org",
            "@alice example:example.org",
            "@alice\tbob:example.org",
            "@alice\nbob:example.org",
            "@álîçé:example.org",
            "@:example.org",
        ],
    )
    def test_historical_matrix_user_id_accepts_historical_localparts(self, matrix_id: str) -> None:
        """Identity binding should accept historical Matrix localparts."""
        assert parse_historical_matrix_user_id(matrix_id) == matrix_id

    def test_parse_invalid_matrix_id(self) -> None:
        """Test parsing invalid Matrix IDs."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            MatrixID.parse("invalid")

        with pytest.raises(ValueError, match="Invalid Matrix ID, missing domain"):
            MatrixID.parse("@nodomainpart")

        with pytest.raises(ValueError, match="Invalid Matrix ID localpart"):
            MatrixID.parse("@alice\x00:example.org")

    @pytest.mark.parametrize(
        "matrix_id",
        [
            "@:example.org",
            "@Alice:example.org",
            "@alice example:example.org",
            "@alice:example.org extra",
            "@alice:",
            "@alice:example.org:",
            "@alice:example.org:123456",
            "@alice:example.org.",
            "@alice:example..org",
            "@alice:.example.org",
            "@alice:[1234:5678::abcd",
            "@alice:[1234:5678::abcd]extra",
            "@alice:[::::]",
            "@alice:[fe80::1%eth0]",
            "@alice:exa mple.org",
            "@" + ("a" * 250) + ":example.org",
        ],
    )
    def test_current_matrix_user_id_rejects_invalid_current_grammar(self, matrix_id: str) -> None:
        """Malformed Matrix user IDs must fail before identity-sensitive code trusts them."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            parse_current_matrix_user_id(matrix_id)

    @pytest.mark.parametrize(
        "matrix_id",
        [
            "@alice:example.org extra",
            "@alice:",
            "@alice:example.org:",
            "@alice:example.org:123456",
            "@alice:example.org.",
            "@alice:example..org",
            "@alice:.example.org",
            "@alice:[::::]",
            "@alice:[fe80::1%eth0]",
            "@" + ("a" * 250) + ":example.org",
        ],
    )
    def test_historical_matrix_user_id_rejects_invalid_server_or_shape(self, matrix_id: str) -> None:
        """Historical localparts do not loosen server-name or length checks."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            parse_historical_matrix_user_id(matrix_id)

    def test_historical_matrix_user_id_rejects_surrogate_localpart(self) -> None:
        """Historical localparts still reject surrogate code points."""
        matrix_id = "@alice\ud800:example.org"
        with pytest.raises(ValueError, match="Invalid Matrix ID localpart"):
            parse_historical_matrix_user_id(matrix_id)

        assert try_parse_historical_matrix_user_id(matrix_id) is None

    def test_from_username(self) -> None:
        """Test creating MatrixID from a concrete Matrix username."""
        mid = MatrixID.from_username("mindroom_calculator", "localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_agent_name_extraction(self, tmp_path: Path) -> None:
        """Test extracting entity name."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "actual_calculator", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert _entity_name(f"@actual_calculator:{domain}", self.config, runtime_paths) == "calculator"
        assert _entity_name(f"@user:{domain}", self.config, runtime_paths) is None
        assert _entity_name(f"@mindroom_unknown:{domain}", self.config, runtime_paths) is None

    def test_parse_router(self, tmp_path: Path) -> None:
        """Test parsing a router agent ID."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_router", "actual_router", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        mid = MatrixID.parse(f"@actual_router:{domain}")
        assert mid.username == "actual_router"
        assert mid.domain == domain
        assert mid.full_id == f"@actual_router:{domain}"
        assert _entity_name(mid.full_id, self.config, runtime_paths) == "router"

    def test_namespaced_agent_localpart_and_parsing(self, tmp_path: Path) -> None:
        """Namespaced generated localparts are provisioning proposals, not runtime aliases."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = Config(
            agents=self.config.agents,
            teams=self.config.teams,
            room_models=self.config.room_models,
            models=self.config.models,
        )
        config = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
        domain = config.get_domain(runtime_paths)
        persist_entity_accounts(config, runtime_paths, usernames={"calculator": "actual_calculator"})

        assert agent_username_localpart("calculator", runtime_paths=runtime_paths) == "mindroom_calculator_a1b2c3d4"
        assert _entity_name(f"@mindroom_calculator_a1b2c3d4:{domain}", config, runtime_paths) is None

        assert _entity_name(f"@actual_calculator:{domain}", config, runtime_paths) == "calculator"
        assert _entity_name(f"@mindroom_calculator:{domain}", config, runtime_paths) is None


class TestThreadStateKey:
    """Test the ThreadStateKey class."""

    def test_parse_state_key(self) -> None:
        """Test parsing a state key."""
        key = _ThreadStateKey.parse("$thread123:calculator")
        assert key.thread_id == "$thread123"
        assert key.agent_name == "calculator"
        assert key.key == "$thread123:calculator"

    def test_parse_invalid_state_key(self) -> None:
        """Test parsing invalid state keys."""
        with pytest.raises(ValueError, match="Invalid state key"):
            _ThreadStateKey.parse("invalid")

    def test_create_state_key(self) -> None:
        """Test creating a state key."""
        key = _ThreadStateKey("$thread456", "general")
        assert key.thread_id == "$thread456"
        assert key.agent_name == "general"
        assert key.key == "$thread456:general"


class TestHelperFunctions:
    """Test helper functions."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test__is_entity_id(self, tmp_path: Path) -> None:
        """Test quick agent ID check."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "actual_calculator", "pw", domain=domain)
        state.add_account("agent_general", "actual_general", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert _is_entity_id(f"@actual_calculator:{domain}", self.config, runtime_paths) is True
        assert _is_entity_id(f"@actual_general:{domain}", self.config, runtime_paths) is True
        assert _is_entity_id(f"@user:{domain}", self.config, runtime_paths) is False
        assert _is_entity_id(f"@mindroom_general:{domain}", self.config, runtime_paths) is False
        assert _is_entity_id(f"@mindroom_unknown:{domain}", self.config, runtime_paths) is False

    def test__entity_name(self, tmp_path: Path) -> None:
        """Test entity name extraction."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "actual_calculator", "pw", domain=domain)
        state.add_account("agent_general", "actual_general", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert _entity_name(f"@actual_calculator:{domain}", self.config, runtime_paths) == "calculator"
        assert _entity_name(f"@actual_general:{domain}", self.config, runtime_paths) == "general"
        assert _entity_name(f"@user:{domain}", self.config, runtime_paths) is None
        assert _entity_name("invalid", self.config, runtime_paths) is None

    def test_entity_name_trusts_persisted_current_username_drift(self, tmp_path: Path) -> None:
        """Persisted usernames for current managed agents should resolve to their entity name."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "mindroom_calculator", "pw", domain=domain)
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert (
            _entity_name(
                f"@mindroom_general_oldns:{domain}",
                self.config,
                runtime_paths,
            )
            == "general"
        )
        assert _is_entity_id(f"@mindroom_general_oldns:{domain}", self.config, runtime_paths) is True

    def test_managed_account_user_id_resolves_persisted_username(self, tmp_path: Path) -> None:
        """Managed account user-id resolution should preserve persisted username drift."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        account_key = managed_account_key("general")
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account(account_key, "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert account_key == "agent_general"
        assert managed_account_user_id(account_key, domain, runtime_paths) == f"@mindroom_general_oldns:{domain}"
        assert managed_account_user_id(managed_account_key("missing"), domain, runtime_paths) is None

    def test_config_entity_ids_require_persisted_current_usernames(self, tmp_path: Path) -> None:
        """Config entity IDs should use only persisted runtime account usernames."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_router", "mindroom_router", "pw", domain=domain)
        state.add_account("agent_calculator", "actual_calculator", "pw", domain=domain)
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        ids = entity_ids(self.config, runtime_paths)

        assert ids["general"].full_id == f"@mindroom_general_oldns:{domain}"
        assert ids["calculator"].full_id == f"@actual_calculator:{domain}"

    def test_matrix_state_load_migrates_legacy_accounts_to_current_schema(self, tmp_path: Path) -> None:
        """Loading legacy state should backfill the current domain and drop old compatibility fields."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            yaml.safe_dump(
                {
                    "accounts": {
                        "agent_general": {
                            "username": "mindroom_general_oldns",
                            "password": "pw",
                            "known_user_ids": ["@mindroom_general_oldns:legacy.example.com"],
                        },
                    },
                },
                sort_keys=False,
            ),
        )

        state = MatrixState.load(runtime_paths=runtime_paths)

        assert state.accounts["agent_general"].domain == self.config.get_domain(runtime_paths)
        migrated_data = yaml.safe_load(state_file.read_text())
        assert migrated_data["accounts"]["agent_general"]["domain"] == self.config.get_domain(runtime_paths)
        assert "known_user_ids" not in migrated_data["accounts"]["agent_general"]

    def test_matrix_state_load_preserves_persisted_account_domain(self, tmp_path: Path) -> None:
        """Loading current state must preserve actual provisioned account domains."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            yaml.safe_dump(
                {
                    "accounts": {
                        "agent_general": {
                            "username": "actual_general",
                            "password": "pw",
                            "domain": "matrix.example",
                        },
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        state = MatrixState.load(runtime_paths=runtime_paths)

        assert state.accounts["agent_general"].domain == "matrix.example"
        assert managed_account_user_id("agent_general", self.config.get_domain(runtime_paths), runtime_paths) == (
            "@actual_general:matrix.example"
        )

    def test_matrix_state_load_migrates_without_advisory_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Matrix state loads should stay lock-free even when normalizing old files."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            yaml.safe_dump(
                {
                    "accounts": {
                        "agent_general": {
                            "username": "mindroom_general_oldns",
                            "password": "pw",
                        },
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        def _fail_flock(*_args: object, **_kwargs: object) -> None:
            raise AssertionError

        monkeypatch.setattr(fcntl, "flock", _fail_flock)

        state = MatrixState.load(runtime_paths=runtime_paths)

        assert state.accounts["agent_general"].domain == self.config.get_domain(runtime_paths)
        assert not state_file.with_name("matrix_state.yaml.lock").exists()

    def test_matrix_state_save_is_atomic_without_advisory_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Matrix state saves should use temp-file replacement, not blocking file locks."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)
        domain = self.config.get_domain(runtime_paths)

        def _fail_flock(*_args: object, **_kwargs: object) -> None:
            raise AssertionError

        monkeypatch.setattr(fcntl, "flock", _fail_flock)

        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_general", "mindroom_general", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert yaml.safe_load(state_file.read_text(encoding="utf-8"))["accounts"]["agent_general"]["domain"] == domain
        assert not state_file.with_name("matrix_state.yaml.lock").exists()

    def test_matrix_state_save_keeps_existing_file_when_temp_write_is_interrupted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed Matrix state write should not leave partial YAML behind."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)

        original_state = MatrixState.load(runtime_paths=runtime_paths)
        original_state.add_account("agent_general", "mindroom_general", "pw", domain=domain)
        original_state.save(runtime_paths=runtime_paths)
        original_contents = state_file.read_text(encoding="utf-8")

        class _InterruptedWriteError(RuntimeError):
            """Sentinel write failure used to simulate an interrupted persistence attempt."""

        def _partial_dump(*args: object, **kwargs: object) -> None:  # noqa: ARG001
            file_obj = args[1]
            assert hasattr(file_obj, "write")
            file_obj.write("accounts:\n  partial")
            file_obj.flush()
            raise _InterruptedWriteError

        replacement_state = MatrixState.load(runtime_paths=runtime_paths)
        replacement_state.add_account("agent_other", "mindroom_other", "pw", domain=domain)
        monkeypatch.setattr("mindroom.matrix.state.yaml.safe_dump", _partial_dump)

        with pytest.raises(_InterruptedWriteError):
            replacement_state.save(runtime_paths=runtime_paths)

        assert state_file.read_text(encoding="utf-8") == original_contents
        assert MatrixState.load(runtime_paths=runtime_paths).accounts["agent_general"].username == "mindroom_general"

    def test_entity_name_ignores_removed_persisted_username(self, tmp_path: Path) -> None:
        """Persisted usernames for removed agents must not stay live-managed."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_removed", "mindroom_removed", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert _entity_name(f"@mindroom_removed:{domain}", self.config, runtime_paths) is None
        assert _is_entity_id(f"@mindroom_removed:{domain}", self.config, runtime_paths) is False
