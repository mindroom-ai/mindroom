"""Test configuration and fixtures for MindRoom tests."""

import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio
from aioresponses import aioresponses

import mindroom.bot  # noqa: F401
from mindroom.bot import AgentBot, TeamBot
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest, SendTextRequest
from mindroom.edit_regenerator import EditRegenerator
from mindroom.matrix.cache.event_cache import _EventCache
from mindroom.matrix.cache.write_coordinator import _EventCacheWriteCoordinator
from mindroom.matrix.client import (
    DeliveredMatrixEvent,
    ResolvedVisibleMessage,
    build_edit_event_content,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest, ResponseRunner
from mindroom.turn_controller import TurnController
from mindroom.turn_policy import TurnPolicy
from mindroom.turn_store import TurnStore

__all__ = [
    "TEST_ACCESS_TOKEN",
    "TEST_PASSWORD",
    "FakeCredentialsManager",
    "aioresponse",
    "bind_runtime_paths",
    "build_private_template_dir",
    "bypass_authorization",
    "create_mock_room",
    "delivered_matrix_event",
    "delivered_matrix_side_effect",
    "install_edit_message_mock",
    "install_generate_response_mock",
    "install_runtime_cache_support",
    "install_send_response_mock",
    "install_send_skill_command_response_mock",
    "make_event_cache_mock",
    "make_event_cache_write_coordinator_mock",
    "make_matrix_client_mock",
    "make_visible_message",
    "normalize_console_output",
    "orchestrator_runtime_paths",
    "patch_response_runner_module",
    "replace_delivery_gateway_deps",
    "replace_edit_regenerator_deps",
    "replace_response_runner_deps",
    "replace_turn_controller_deps",
    "replace_turn_policy_deps",
    "replace_turn_store_deps",
    "resolve_response_thread_root_for_test",
    "runtime_paths_for",
    "sync_bot_runtime_state",
    "test_runtime_paths",
    "unwrap_extracted_collaborator",
    "wrap_extracted_collaborators",
]

_TEST_RUNTIME_PATHS_BY_CONFIG_ID: dict[int, RuntimePaths] = {}
_VISIBLE_MESSAGE_IDS = count(1)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SOFT_WRAP_RE = re.compile(r"(?<=\S)\n(?=\S)")
RuntimeBot = AgentBot | TeamBot


async def _empty_async_iterator() -> AsyncGenerator[object, None]:
    """Yield nothing while preserving async-iterator semantics for nio relations APIs."""
    if False:
        yield None


def _make_room_get_event_response(event_id: str) -> nio.RoomGetEventResponse:
    """Return a minimal RoomGetEventResponse containing one visible text event."""
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = "@user:localhost"
    event.body = event_id
    event.server_timestamp = 0
    event.source = {
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": event_id,
        },
    }
    response = nio.RoomGetEventResponse()
    response.event = event
    return response


def make_matrix_client_mock(*, user_id: str = "@mindroom_test:example.com") -> AsyncMock:
    """Return an AsyncClient-shaped mock with safe defaults for sync nio APIs."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = {}
    client.user_id = user_id
    client.next_batch = "s_test_token"
    presence_response = MagicMock()
    presence_response.presence = "offline"
    presence_response.last_active_ago = 3_600_000
    room_messages_response = nio.RoomMessagesResponse(room_id="!test:localhost", chunk=[], start="", end=None)
    client.add_event_callback = MagicMock()
    client.add_response_callback = MagicMock()
    client.get_presence = AsyncMock(return_value=presence_response)
    client.room_get_event = AsyncMock(side_effect=lambda _room_id, event_id: _make_room_get_event_response(event_id))
    client.room_get_event_relations = MagicMock(return_value=_empty_async_iterator())
    client.room_messages = AsyncMock(return_value=room_messages_response)
    return client


def delivered_matrix_event(
    event_id: str,
    content: Mapping[str, object] | None = None,
) -> DeliveredMatrixEvent:
    """Return one delivered Matrix event using the exact content payload seen by the helper."""
    return DeliveredMatrixEvent(
        event_id=event_id,
        content_sent={} if content is None else dict(content),
    )


def delivered_matrix_side_effect(event_id: str) -> Callable[..., Awaitable[DeliveredMatrixEvent]]:
    """Build one async mock side effect that mirrors send/edit helpers returning delivered events."""

    async def _deliver(*args: object, **kwargs: object) -> DeliveredMatrixEvent:
        if "content" in kwargs:
            content = kwargs["content"]
            content_mapping = content if isinstance(content, Mapping) else None
            return delivered_matrix_event(event_id, content_mapping)
        if "new_content" in kwargs:
            new_content = kwargs["new_content"]
            new_text = kwargs.get("new_text")
            if isinstance(new_content, Mapping) and isinstance(new_text, str):
                return delivered_matrix_event(
                    event_id,
                    build_edit_event_content(
                        event_id=str(args[2]) if len(args) > 2 else "",
                        new_content=dict(new_content),
                        new_text=new_text,
                    ),
                )
            content = new_content
            content_mapping = content if isinstance(content, Mapping) else None
            return delivered_matrix_event(event_id, content_mapping)
        if len(args) > 4 and isinstance(args[3], Mapping) and isinstance(args[4], str):
            return delivered_matrix_event(
                event_id,
                build_edit_event_content(
                    event_id=str(args[2]),
                    new_content=dict(args[3]),
                    new_text=args[4],
                ),
            )
        content_index = 2 if len(args) <= 3 else 3
        content = args[content_index] if len(args) > content_index else None
        content_mapping = content if isinstance(content, Mapping) else None
        return delivered_matrix_event(event_id, content_mapping)

    return _deliver


def make_event_cache_mock() -> AsyncMock:
    """Return an async mock shaped like the event cache protocol."""
    event_cache = AsyncMock(spec=_EventCache)
    event_cache.get_event.return_value = None
    event_cache.get_latest_edit.return_value = None
    event_cache.get_thread_events.return_value = None
    event_cache.get_thread_cache_state.return_value = None
    event_cache.get_thread_id_for_event.return_value = None
    event_cache.append_event.return_value = True
    event_cache.redact_event.return_value = False
    return event_cache


def make_event_cache_write_coordinator_mock(*, owner: object | None = None) -> _EventCacheWriteCoordinator:
    """Return a coordinator-shaped runtime helper with the real synchronous queue contract."""
    return _EventCacheWriteCoordinator(
        logger=MagicMock(),
        background_task_owner=object() if owner is None else owner,
    )


def install_runtime_cache_support(bot: RuntimeBot) -> RuntimeBot:
    """Attach required cache runtime support to one test bot."""
    if bot._runtime_view.event_cache is None:
        bot.event_cache = make_event_cache_mock()
    if bot._runtime_view.event_cache_write_coordinator is None:
        bot.event_cache_write_coordinator = make_event_cache_write_coordinator_mock(owner=bot._runtime_view)
    sync_bot_runtime_state(bot)
    return bot


def normalize_console_output(text: str) -> str:
    """Collapse wrapped console output for stable substring assertions."""
    return " ".join(_SOFT_WRAP_RE.sub("", _ANSI_RE.sub("", text)).split())


class _ExtractedCollaboratorProxy[CollaboratorT]:
    """Mutable proxy that keeps real collaborator attributes visible to tests."""

    def __init__(self, wrapped: CollaboratorT) -> None:
        object.__setattr__(self, "_wrapped", wrapped)

    def __getattr__(self, name: str) -> object:
        proxy_dict = object.__getattribute__(self, "__dict__")
        if name in proxy_dict:
            return proxy_dict[name]
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value: object) -> None:
        object.__getattribute__(self, "__dict__")[name] = value

    def __delattr__(self, name: str) -> None:
        proxy_dict = object.__getattribute__(self, "__dict__")
        if name in proxy_dict:
            del proxy_dict[name]
            return
        msg = f"{type(self).__name__!s} has no attribute {name!r}"
        raise AttributeError(msg)


class FakeCredentialsManager:
    """Stub credentials manager for tests that need credential lookup."""

    def __init__(
        self,
        credentials_by_service: dict[str, dict[str, object]],
        worker_managers: dict[str, "FakeCredentialsManager"] | None = None,
        *,
        storage_root: Path | None = None,
        current_worker_key: str | None = None,
        current_worker_root: Path | None = None,
    ) -> None:
        self._credentials_by_service = credentials_by_service
        self._worker_managers = worker_managers or {}
        self.storage_root = storage_root or Path("/var/empty/mindroom-fake-storage")
        self.base_path = self.storage_root / "credentials"
        self.shared_base_path = self.base_path
        self.current_worker_key = current_worker_key
        self.current_worker_root = current_worker_root

    def load_credentials(self, service: str) -> dict[str, object]:
        """Return stored credentials for *service*, or empty dict."""
        return self._credentials_by_service.get(service, {})

    def for_worker(self, worker_key: str) -> "FakeCredentialsManager":
        """Return a worker-scoped credentials manager."""
        return self._worker_managers.get(
            worker_key,
            FakeCredentialsManager(
                {},
                storage_root=self.storage_root / "workers" / worker_key,
                current_worker_key=worker_key,
                current_worker_root=self.storage_root / "workers" / worker_key,
            ),
        )

    def shared_manager(self) -> "FakeCredentialsManager":
        """Return the shared credential layer for this fake manager."""
        return self


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests marked with requires_matrix unless MATRIX_SERVER_URL is set."""
    if os.environ.get("MATRIX_SERVER_URL"):
        # Matrix server available, don't skip
        return

    skip_marker = pytest.mark.skip(reason="requires_matrix: no MATRIX_SERVER_URL set")
    for item in items:
        if "requires_matrix" in item.keywords:
            item.add_marker(skip_marker)


# Test credentials constants - not real credentials, safe for testing
TEST_PASSWORD = "mock_test_password"  # noqa: S105
TEST_ACCESS_TOKEN = "mock_test_token"  # noqa: S105


def test_runtime_paths(tmp_root: Path) -> RuntimePaths:
    """Create an isolated runtime context for one test config."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    config_path = tmp_root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_root / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


test_runtime_paths.__test__ = False


def orchestrator_runtime_paths(
    storage_path: Path,
    *,
    config_path: Path | None = None,
) -> RuntimePaths:
    """Build an explicit runtime context for orchestrator tests.

    Default the config path to an isolated file under the provided test root so
    callers never fall back to the tracked repo-root config.yaml.
    """
    if config_path is None:
        config_path = storage_path / "config.yaml"
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def bind_runtime_paths(
    config: Config,
    runtime_paths: RuntimePaths,
) -> Config:
    """Return a runtime-bound copy of a test config."""
    bound = Config.validate_with_runtime(config.authored_model_dump(), runtime_paths)
    authored_coalescing = config.defaults.coalescing
    if "debounce_ms" not in authored_coalescing.model_fields_set:
        bound.defaults.coalescing.debounce_ms = 0
    if "upload_grace_ms" not in authored_coalescing.model_fields_set:
        bound.defaults.coalescing.upload_grace_ms = 0
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID[id(bound)] = runtime_paths
    return bound


def runtime_paths_for(config: Config) -> RuntimePaths:
    """Return the explicit runtime context previously bound to a test config."""
    runtime_paths = _TEST_RUNTIME_PATHS_BY_CONFIG_ID.get(id(config))
    if runtime_paths is None:
        msg = "Test config is missing bound RuntimePaths"
        raise KeyError(msg)
    return runtime_paths


def create_mock_room(
    room_id: str = "!test:localhost",
    agents: list[str] | None = None,
    config: Config | None = None,
) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        domain = config.get_domain(runtime_paths_for(config)) if config is not None else "localhost"
        room.users = {f"@mindroom_{agent}:{domain}": None for agent in agents}
    else:
        room.users = {}
    return room


def make_visible_message(
    *,
    sender: str = "@user:localhost",
    body: str = "",
    event_id: str | None = None,
    timestamp: int = 0,
    content: dict[str, object] | None = None,
    thread_id: str | None = None,
) -> ResolvedVisibleMessage:
    """Build one typed visible message for thread/history tests."""
    resolved_content = dict(content) if isinstance(content, dict) else {}
    if "body" not in resolved_content and body:
        resolved_content["body"] = body
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id or f"$visible-{next(_VISIBLE_MESSAGE_IDS)}",
        timestamp=timestamp,
        content=resolved_content or None,
        thread_id=thread_id,
    )


def resolve_response_thread_root_for_test(
    thread_id: str | None,
    _reply_to_event_id: str | None,
    *,
    room_id: str,
    response_envelope: object | None = None,
) -> str | None:
    """Resolve thread roots like the bot seam helpers used by response tests."""
    del room_id
    if response_envelope is not None:
        return response_envelope.target.resolved_thread_id
    return thread_id


def unwrap_extracted_collaborator[T](collaborator: T) -> T:
    """Return the real extracted collaborator behind one test wrapper."""
    if isinstance(collaborator, MagicMock):
        wrapped = collaborator._mock_wraps
        if wrapped is not None:
            return wrapped
    wrapped = getattr(collaborator, "_wrapped", None)
    if wrapped is not None:
        return wrapped
    return collaborator


def wrap_extracted_collaborators(bot: RuntimeBot, *names: str) -> RuntimeBot:
    """Wrap frozen extracted collaborators so tests can patch their methods."""
    sync_bot_runtime_state(bot)
    collaborator_names = names or (
        "_turn_policy",
        "_delivery_gateway",
        "_response_runner",
        "_turn_store",
        "_edit_regenerator",
        "_inbound_turn_normalizer",
        "_conversation_resolver",
        "_conversation_state_writer",
    )
    for name in collaborator_names:
        collaborator = getattr(bot, name)
        if isinstance(collaborator, MagicMock | _ExtractedCollaboratorProxy):
            continue
        setattr(bot, name, _ExtractedCollaboratorProxy(collaborator))
    return bot


def sync_bot_runtime_state(bot: RuntimeBot) -> None:
    """Update the extracted runtime state after tests mutate bot internals."""
    runtime = bot._runtime_view
    client = bot.client
    if client is not None and getattr(client, "user_id", None) is None:
        client.user_id = bot.matrix_id.full_id
    runtime.client = bot.client
    runtime.config = bot.config
    runtime.enable_streaming = bot.enable_streaming
    runtime.orchestrator = bot.orchestrator


def replace_turn_policy_deps(bot: RuntimeBot, **changes: object) -> TurnPolicy:
    """Rebuild the turn policy after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    policy = unwrap_extracted_collaborator(bot._turn_policy)
    policy_field_names = set(policy.deps.__dataclass_fields__)
    policy_changes = {name: value for name, value in changes.items() if name in policy_field_names}
    rebuilt = TurnPolicy(replace(policy.deps, **policy_changes)) if policy_changes else policy
    bot._turn_policy = rebuilt
    wrap_extracted_collaborators(bot, "_turn_policy")
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    controller_field_names = set(controller.deps.__dataclass_fields__)
    controller_changes = {name: value for name, value in changes.items() if name in controller_field_names}
    if policy_changes:
        controller_changes["turn_policy"] = bot._turn_policy
    if store_changes:
        controller_changes["turn_store"] = bot._turn_store
    if controller_changes:
        replace_turn_controller_deps(bot, **controller_changes)
    return rebuilt


def replace_turn_store_deps(bot: RuntimeBot, **changes: object) -> TurnStore:
    """Rebuild the turn store after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    store = unwrap_extracted_collaborator(bot._turn_store)
    rebuilt = TurnStore(replace(store.deps, **changes))
    bot._turn_store = rebuilt
    wrap_extracted_collaborators(bot, "_turn_store")
    return rebuilt


def replace_delivery_gateway_deps(bot: RuntimeBot, **changes: object) -> DeliveryGateway:
    """Rebuild the delivery gateway after swapping captured collaborators."""
    sync_bot_runtime_state(bot)
    gateway = unwrap_extracted_collaborator(bot._delivery_gateway)
    rebuilt = DeliveryGateway(replace(gateway.deps, **changes))
    bot._delivery_gateway = rebuilt
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)
    return rebuilt


def replace_response_runner_deps(bot: RuntimeBot, **changes: object) -> ResponseRunner:
    """Rebuild the response runner after swapping captured collaborators."""
    sync_bot_runtime_state(bot)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    rebuilt = ResponseRunner(replace(coordinator.deps, **changes))
    bot._response_runner = rebuilt
    wrap_extracted_collaborators(bot, "_response_runner")
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)
    return rebuilt


def replace_edit_regenerator_deps(bot: RuntimeBot, **changes: object) -> EditRegenerator:
    """Rebuild the edit regenerator after swapping captured collaborators."""
    install_runtime_cache_support(bot)
    regenerator = unwrap_extracted_collaborator(bot._edit_regenerator)
    regenerator_field_names = set(regenerator.deps.__dataclass_fields__)
    rebuilt_changes = {
        name: value for name, value in changes.items() if name in regenerator_field_names or name == "logger"
    }
    if "logger" in rebuilt_changes:
        logger = rebuilt_changes.pop("logger")
        rebuilt_changes["get_logger"] = lambda logger=logger: logger
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    rebuilt = EditRegenerator(replace(regenerator.deps, **rebuilt_changes))
    bot._edit_regenerator = rebuilt
    wrap_extracted_collaborators(bot, "_edit_regenerator")
    replace_turn_controller_deps(bot, edit_regenerator=bot._edit_regenerator)
    return rebuilt


def replace_turn_controller_deps(bot: RuntimeBot, **changes: object) -> TurnController:
    """Rebuild the turn controller after swapping collaborators captured at construction."""
    sync_bot_runtime_state(bot)
    controller = unwrap_extracted_collaborator(bot._turn_controller)
    controller_field_names = set(controller.deps.__dataclass_fields__)
    rebuilt_changes = {name: value for name, value in changes.items() if name in controller_field_names}
    default_collaborators = {
        "conversation_cache": "_conversation_cache",
        "resolver": "_conversation_resolver",
        "normalizer": "_inbound_turn_normalizer",
        "turn_policy": "_turn_policy",
        "ingress_hook_runner": "_ingress_hook_runner",
        "response_runner": "_response_runner",
        "delivery_gateway": "_delivery_gateway",
        "tool_runtime": "_tool_runtime_support",
        "turn_store": "_turn_store",
        "edit_regenerator": "_edit_regenerator",
    }
    for field_name, attr_name in default_collaborators.items():
        if field_name in rebuilt_changes:
            continue
        rebuilt_changes[field_name] = getattr(bot, attr_name)
    store_field_names = set(unwrap_extracted_collaborator(bot._turn_store).deps.__dataclass_fields__)
    store_changes = {name: value for name, value in changes.items() if name in store_field_names}
    if store_changes:
        replace_turn_store_deps(bot, **store_changes)
        rebuilt_changes["turn_store"] = bot._turn_store
    if "edit_regenerator" not in rebuilt_changes:
        rebuilt_changes["edit_regenerator"] = bot._edit_regenerator
    rebuilt = TurnController(replace(controller.deps, **rebuilt_changes))
    bot._turn_controller = rebuilt
    edit_changes = {
        name: value
        for name, value in changes.items()
        if name in unwrap_extracted_collaborator(bot._edit_regenerator).deps.__dataclass_fields__
    }
    if edit_changes:
        replace_edit_regenerator_deps(bot, **edit_changes)
    return rebuilt


@contextmanager
def patch_response_runner_module(**changes: object) -> Generator[None, None, None]:
    """Patch module-level response coordinator seams on the real current owner."""
    with ExitStack() as stack:
        for name, replacement in changes.items():
            module_name = (
                "mindroom.response_lifecycle" if name == "apply_post_response_effects" else "mindroom.response_runner"
            )
            stack.enter_context(patch(f"{module_name}.{name}", new=replacement))
        yield


def install_send_response_mock(bot: RuntimeBot, send_response: AsyncMock) -> None:
    """Route visible delivery through one legacy-style send-response mock."""
    wrap_extracted_collaborators(bot, "_delivery_gateway")

    async def _send_text(request: SendTextRequest) -> str | None:
        return await send_response(
            request.target.room_id,
            request.target.reply_to_event_id,
            request.response_text,
            request.target.thread_id,
            reply_to_event=None,
            skip_mentions=request.skip_mentions,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
            thread_mode_override=None,
            target=request.target,
        )

    bot._delivery_gateway.send_text = AsyncMock(side_effect=_send_text)
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


def install_generate_response_mock(bot: RuntimeBot, generate_response: AsyncMock) -> None:
    """Route response execution through one legacy-style generate-response mock."""
    wrap_extracted_collaborators(bot, "_response_runner")

    async def _generate(request: ResponseRequest) -> str | None:
        if request.prepare_after_lock is not None:
            try:
                request = await request.prepare_after_lock(request)
            except Exception as exc:
                raise PostLockRequestPreparationError from exc
        attachment_ids = list(request.attachment_ids) if request.attachment_ids is not None else None
        return await generate_response(
            room_id=request.room_id,
            prompt=request.prompt,
            reply_to_event_id=request.reply_to_event_id,
            thread_id=request.thread_id,
            thread_history=request.thread_history,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
            user_id=request.user_id,
            media=request.media,
            attachment_ids=attachment_ids,
            model_prompt=request.model_prompt,
            strip_transient_enrichment_after_run=request.strip_transient_enrichment_after_run,
            system_enrichment_items=request.system_enrichment_items,
            response_envelope=request.response_envelope,
            correlation_id=request.correlation_id,
            target=request.target,
            matrix_run_metadata=request.matrix_run_metadata,
        )

    bot._response_runner.generate_response = AsyncMock(side_effect=_generate)
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)


def install_edit_message_mock(bot: RuntimeBot, edit_message: AsyncMock) -> None:
    """Route Matrix edits through one legacy-style edit-message mock."""
    wrap_extracted_collaborators(bot, "_delivery_gateway")

    async def _edit_text(request: EditTextRequest) -> bool:
        return await edit_message(
            request.target.room_id,
            request.event_id,
            request.new_text,
            request.target.thread_id,
            tool_trace=request.tool_trace,
            extra_content=request.extra_content,
        )

    bot._delivery_gateway.edit_text = AsyncMock(side_effect=_edit_text)
    replace_turn_controller_deps(bot, delivery_gateway=bot._delivery_gateway)
    replace_response_runner_deps(bot, delivery_gateway=bot._delivery_gateway)


def install_send_skill_command_response_mock(bot: RuntimeBot, send_skill_command_response: AsyncMock) -> None:
    """Route skill-command dispatch through one test mock on the real owner."""
    wrap_extracted_collaborators(bot, "_response_runner")
    bot._response_runner.send_skill_command_response = send_skill_command_response
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)


@pytest.fixture
def build_private_template_dir(tmp_path: Path) -> Callable[..., Path]:
    """Return a helper that creates a local private-instance template directory."""

    def _build(
        name: str = "private_template",
        *,
        files: dict[str, str] | None = None,
    ) -> Path:
        template_dir = tmp_path / name
        template_dir.mkdir(parents=True, exist_ok=True)
        template_files = files or {
            "SOUL.md": "Template soul.\n",
            "USER.md": "Template user.\n",
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        }
        for relative_path, content in template_files.items():
            destination = template_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        return template_dir

    return _build


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m


@pytest.fixture(autouse=True)
def _pin_matrix_homeserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runtime defaults isolated from shell-level runtime overrides.

    Tests use ':localhost' Matrix IDs and non-namespaced localparts unless they
    explicitly opt into a different runtime context.
    """
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_SERVER_NAME", raising=False)
    monkeypatch.delenv("MINDROOM_NAMESPACE", raising=False)
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


@pytest.fixture(autouse=True)
def _reset_runtime_paths() -> Generator[None, None, None]:
    """Restore process env and bound test runtime mappings after each test."""
    original_env = os.environ.copy()
    original_bound_configs = dict(_TEST_RUNTIME_PATHS_BY_CONFIG_ID)
    yield
    os.environ.clear()
    os.environ.update(original_env)
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.clear()
    _TEST_RUNTIME_PATHS_BY_CONFIG_ID.update(original_bound_configs)


@pytest.fixture(autouse=True)
def bypass_authorization(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Bypass authorization checks in tests by default.

    This allows test users like @user:example.com to interact with agents
    without needing to be in the authorized_users list.

    Tests in test_authorization.py are excluded since they test authorization itself.
    """
    # Don't bypass authorization for tests that are specifically testing it
    if "test_authorization" in request.node.parent.name:
        yield
    else:
        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        ):
            yield
