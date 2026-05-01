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
from mindroom.matrix.identity import (
    MatrixID,
    _ThreadStateKey,
    extract_agent_name,
    is_agent_id,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix_identifiers import agent_username_localpart
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _bind_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, test_runtime_paths(tmp_path))


class TestMatrixID:
    """Test the MatrixID class."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
                "router": AgentConfig(display_name="Router", rooms=["#test:example.org"]),
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

    def test_parse_invalid_matrix_id(self) -> None:
        """Test parsing invalid Matrix IDs."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            MatrixID.parse("invalid")

        with pytest.raises(ValueError, match="Invalid Matrix ID, missing domain"):
            MatrixID.parse("@nodomainpart")

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
            "@alice:[1234:5678::abcd",
            "@alice:[1234:5678::abcd]extra",
            "@alice:exa mple.org",
            "@" + ("a" * 250) + ":example.org",
        ],
    )
    def test_parse_rejects_invalid_matrix_user_id_grammar(self, matrix_id: str) -> None:
        """Malformed Matrix user IDs must fail before identity-sensitive code trusts them."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            MatrixID.parse(matrix_id)

    def test_from_agent(self, tmp_path: Path) -> None:
        """Test creating MatrixID from agent name."""
        mid = MatrixID.from_agent(
            "calculator",
            "localhost",
            runtime_paths_for(_bind_runtime_paths(self.config, tmp_path)),
        )
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_agent_name_extraction(self, tmp_path: Path) -> None:
        """Test extracting agent name."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))

        # Valid agent
        mid = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) == "calculator"

        # Not an agent
        mid = MatrixID.parse(f"@user:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) is None

        # Agent prefix but not in config
        mid = MatrixID.parse(f"@mindroom_unknown:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) is None

    def test_parse_router(self, tmp_path: Path) -> None:
        """Test parsing a router agent ID."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        mid = MatrixID.parse(f"@mindroom_router:{domain}")
        assert mid.username == "mindroom_router"
        assert mid.domain == domain
        assert mid.full_id == f"@mindroom_router:{domain}"
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) == "router"

    def test_namespaced_agent_localpart_and_parsing(self, tmp_path: Path) -> None:
        """Agent IDs should use and require configured namespace suffixes."""
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

        assert agent_username_localpart("calculator", runtime_paths=runtime_paths) == "mindroom_calculator_a1b2c3d4"
        namespaced_id = MatrixID.parse(f"@mindroom_calculator_a1b2c3d4:{domain}")
        assert namespaced_id.agent_name(config, runtime_paths) == "calculator"

        legacy_id = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert legacy_id.agent_name(config, runtime_paths) is None


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

    def test_is_agent_id(self, tmp_path: Path) -> None:
        """Test quick agent ID check."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        runtime_paths = runtime_paths_for(self.config)
        assert is_agent_id(f"@mindroom_calculator:{domain}", self.config, runtime_paths) is True
        assert is_agent_id(f"@mindroom_general:{domain}", self.config, runtime_paths) is True
        assert is_agent_id(f"@user:{domain}", self.config, runtime_paths) is False
        # Note: is_agent_id expects valid Matrix IDs - invalid IDs should never reach this function
        assert is_agent_id(f"@mindroom_unknown:{domain}", self.config, runtime_paths) is False

    def test_extract_agent_name(self, tmp_path: Path) -> None:
        """Test agent name extraction."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        runtime_paths = runtime_paths_for(self.config)
        assert extract_agent_name(f"@mindroom_calculator:{domain}", self.config, runtime_paths) == "calculator"
        assert extract_agent_name(f"@mindroom_general:{domain}", self.config, runtime_paths) == "general"
        assert extract_agent_name(f"@user:{domain}", self.config, runtime_paths) is None
        assert extract_agent_name("invalid", self.config, runtime_paths) is None

    def test_extract_agent_name_trusts_persisted_current_username_drift(self, tmp_path: Path) -> None:
        """Persisted usernames for current managed agents should resolve to their agent name."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState()
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert (
            extract_agent_name(
                f"@mindroom_general_oldns:{domain}",
                self.config,
                runtime_paths,
            )
            == "general"
        )
        assert is_agent_id(f"@mindroom_general_oldns:{domain}", self.config, runtime_paths) is True

    def test_matrix_id_agent_name_trusts_persisted_current_username_drift(self, tmp_path: Path) -> None:
        """MatrixID.agent_name should use the same live drift-aware identity seam."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState()
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        drifted_id = MatrixID.parse(f"@mindroom_general_oldns:{domain}")
        assert drifted_id.agent_name(self.config, runtime_paths) == "general"

    def test_matrix_id_agent_name_ignores_configured_id_after_username_drift(self, tmp_path: Path) -> None:
        """Config-derived IDs should stop resolving once a current persisted username exists."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState()
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        configured_id = MatrixID.parse(f"@mindroom_general:{domain}")
        assert configured_id.agent_name(self.config, runtime_paths) is None

    def test_matrix_id_agent_name_ignores_old_domain_sender_ids(self, tmp_path: Path) -> None:
        """MatrixID.agent_name should only trust the current runtime domain."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        state = MatrixState()
        state.add_account("agent_general", "mindroom_general_oldns", "pw", domain="legacy.example.com")
        state.save(runtime_paths=runtime_paths)

        drifted_id = MatrixID.parse("@mindroom_general_oldns:legacy.example.com")
        assert drifted_id.agent_name(self.config, runtime_paths) is None

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

        state = MatrixState()
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

        original_state = MatrixState()
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

        replacement_state = MatrixState()
        replacement_state.add_account("agent_other", "mindroom_other", "pw", domain=domain)
        monkeypatch.setattr("mindroom.matrix.state.yaml.safe_dump", _partial_dump)

        with pytest.raises(_InterruptedWriteError):
            replacement_state.save(runtime_paths=runtime_paths)

        assert state_file.read_text(encoding="utf-8") == original_contents
        assert MatrixState.load(runtime_paths=runtime_paths).accounts["agent_general"].username == "mindroom_general"

    def test_extract_agent_name_ignores_removed_persisted_username(self, tmp_path: Path) -> None:
        """Persisted usernames for removed agents must not stay live-managed."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        runtime_paths = runtime_paths_for(self.config)
        domain = self.config.get_domain(runtime_paths)
        state = MatrixState()
        state.add_account("agent_removed", "mindroom_removed", "pw", domain=domain)
        state.save(runtime_paths=runtime_paths)

        assert extract_agent_name(f"@mindroom_removed:{domain}", self.config, runtime_paths) is None
        assert is_agent_id(f"@mindroom_removed:{domain}", self.config, runtime_paths) is False
