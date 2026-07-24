"""Tests for exact-room report authorization and bounded caching."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.orchestration.report_authorization_runtime import _OriginRoomReportAuthorizer
from mindroom.report_access_policy import ReportAccessPolicy
from mindroom.report_publishing.authorization import (
    OriginRoomAuthorizationKey,
    ReportAuthorizationDecision,
    ReportAuthorizationReason,
    SuccessfulReportAuthorizationCache,
)
from mindroom.report_publishing.store import PublishedReport
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeBot:
    """Minimal live bot surface used by report authorization."""

    client: AsyncMock | None
    running: bool
    matrix_id: object


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
        ),
        runtime_paths,
    )


def _report(config: Config, **changes: object) -> PublishedReport:
    publisher_matrix_user_id = (
        entity_identity_registry(
            config,
            runtime_paths_for(config),
        )
        .current_id("general")
        .full_id
    )
    report = PublishedReport(
        slug="pub_" + ("a" * 32),
        source_type="test",
        source={},
        artifact_kind="html_file",
        artifact_path="reports/example.html",
        title="Example",
        requested_by="@alice:localhost",
        published_by="@alice:localhost",
        published_at="2026-07-23T00:00:00Z",
        public_url=None,
        access_policy=ReportAccessPolicy.ORIGIN_ROOM,
        origin_room_id="!origin:localhost",
        publisher_entity_name="general",
        publisher_matrix_user_id=publisher_matrix_user_id,
    )
    return replace(report, **changes)


def _authorizer(
    config: Config,
    *,
    joined_rooms: list[str] | None = None,
    members: set[str] | None = None,
    cache: SuccessfulReportAuthorizationCache | None = None,
) -> tuple[_OriginRoomReportAuthorizer, AsyncMock]:
    publisher_matrix_id = entity_identity_registry(
        config,
        runtime_paths_for(config),
    ).current_id("general")
    client = AsyncMock(spec=nio.AsyncClient)
    if joined_rooms is None:
        client.joined_rooms.return_value = nio.JoinedRoomsResponse(["!origin:localhost"])
    else:
        client.joined_rooms.return_value = nio.JoinedRoomsResponse(joined_rooms)
    joined_members = members or {publisher_matrix_id.full_id, "@alice:localhost"}
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, "", "") for user_id in joined_members],
        "!origin:localhost",
    )
    bot = _FakeBot(client=client, running=True, matrix_id=publisher_matrix_id)
    return (
        _OriginRoomReportAuthorizer(
            config=config,
            bots={"general": bot},  # type: ignore[dict-item]
            runtime_paths=runtime_paths_for(config),
            cache=cache or SuccessfulReportAuthorizationCache(),
        ),
        client,
    )


@pytest.mark.asyncio
async def test_origin_room_authorization_allows_exact_joined_room(tmp_path: Path) -> None:
    """Viewer and publisher currently joined to exact origin room should pass."""
    config = _config(tmp_path)
    authorizer, _client = _authorizer(config)

    decision = await authorizer.authorize(_report(config), "@alice:localhost")

    assert decision.reason is ReportAuthorizationReason.AUTHORIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("members", "reason"),
    [
        ({"publisher"}, ReportAuthorizationReason.VIEWER_NOT_JOINED),
        ({"@alice:localhost"}, ReportAuthorizationReason.PUBLISHER_NOT_JOINED),
    ],
)
async def test_origin_room_authorization_requires_both_joined_members(
    tmp_path: Path,
    members: set[str],
    reason: ReportAuthorizationReason,
) -> None:
    """Invited, left, banned, or absent identities are absent from joined_members."""
    config = _config(tmp_path)
    publisher_id = _report(config).publisher_matrix_user_id
    concrete_members = {publisher_id if member == "publisher" else member for member in members}
    authorizer, _client = _authorizer(config, members=concrete_members)

    decision = await authorizer.authorize(_report(config), "@alice:localhost")

    assert decision.reason is reason


@pytest.mark.asyncio
async def test_origin_room_authorization_rejects_other_common_room(tmp_path: Path) -> None:
    """Sharing another room with publisher must not authorize origin room."""
    config = _config(tmp_path)
    authorizer, _client = _authorizer(config, joined_rooms=["!other:localhost"])

    decision = await authorizer.authorize(_report(config), "@alice:localhost")

    assert decision.reason is ReportAuthorizationReason.PUBLISHER_NOT_JOINED


@pytest.mark.asyncio
async def test_origin_room_authorization_rejects_stored_publisher_identity_mismatch(tmp_path: Path) -> None:
    """Stored publisher identity must match current configured runtime identity."""
    config = _config(tmp_path)
    authorizer, client = _authorizer(config)

    decision = await authorizer.authorize(
        _report(config, publisher_matrix_user_id="@old-general:localhost"),
        "@alice:localhost",
    )

    assert decision.reason is ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_state", ["missing", "stopped", "client_missing"])
async def test_origin_room_authorization_treats_configured_publisher_outage_as_backend_unavailable(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    """Configured publisher runtime outages should produce a retryable failure."""
    config = _config(tmp_path)
    publisher_matrix_id = entity_identity_registry(
        config,
        runtime_paths_for(config),
    ).current_id("general")
    publisher_bot = _FakeBot(
        client=None if runtime_state == "client_missing" else AsyncMock(spec=nio.AsyncClient),
        running=runtime_state != "stopped",
        matrix_id=publisher_matrix_id,
    )
    bots = {} if runtime_state == "missing" else {"general": publisher_bot}
    authorizer = _OriginRoomReportAuthorizer(
        config=config,
        bots=bots,  # type: ignore[arg-type]
        runtime_paths=runtime_paths_for(config),
    )

    decision = await authorizer.authorize(_report(config), "@alice:localhost")

    assert decision.reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE


@pytest.mark.asyncio
async def test_origin_room_authorization_treats_removed_publisher_as_identity_mismatch(tmp_path: Path) -> None:
    """A publisher removed from current config should remain a denial, not an outage."""
    original_config = _config(tmp_path)
    runtime_paths = runtime_paths_for(original_config)
    current_config = bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="openai", id="gpt-5.6")}),
        runtime_paths,
    )
    authorizer = _OriginRoomReportAuthorizer(
        config=current_config,
        bots={},
        runtime_paths=runtime_paths,
    )

    decision = await authorizer.authorize(_report(original_config), "@alice:localhost")

    assert decision.reason is ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH


@pytest.mark.asyncio
async def test_origin_room_membership_handles_client_teardown_race(tmp_path: Path) -> None:
    """Client removal between precheck and membership lookup should stay typed."""
    config = _config(tmp_path)
    publisher_matrix_id = entity_identity_registry(
        config,
        runtime_paths_for(config),
    ).current_id("general")
    publisher_bot = _FakeBot(client=None, running=True, matrix_id=publisher_matrix_id)

    decision = await _OriginRoomReportAuthorizer._authorize_membership(
        publisher_bot,  # type: ignore[arg-type]
        origin_room_id="!origin:localhost",
        viewer_matrix_user_id="@alice:localhost",
        publisher_matrix_user_id=publisher_matrix_id.full_id,
    )

    assert decision.reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE


@pytest.mark.asyncio
async def test_origin_room_authorization_fails_closed_on_matrix_error(tmp_path: Path) -> None:
    """Matrix transport failures must never become successful authorization."""
    config = _config(tmp_path)
    authorizer, client = _authorizer(config)
    client.joined_members.side_effect = RuntimeError("homeserver unavailable")

    decision = await authorizer.authorize(_report(config), "@alice:localhost")

    assert decision.reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE


@pytest.mark.asyncio
async def test_success_cache_reuses_root_and_assets_until_expiry(tmp_path: Path) -> None:
    """Successful static asset checks should reuse one short membership decision."""
    config = _config(tmp_path)
    now = [100.0]
    cache = SuccessfulReportAuthorizationCache(ttl_seconds=20, monotonic=lambda: now[0])
    authorizer, client = _authorizer(config, cache=cache)
    report = _report(config)

    first = await authorizer.authorize(report, "@alice:localhost")
    second = await authorizer.authorize(report, "@alice:localhost")
    now[0] = 121.0
    third = await authorizer.authorize(report, "@alice:localhost")

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert third.cache_hit is False
    assert client.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_membership_removal_takes_effect_after_success_cache_ttl(tmp_path: Path) -> None:
    """Viewer departure may use cached success only until bounded TTL expires."""
    config = _config(tmp_path)
    now = [100.0]
    cache = SuccessfulReportAuthorizationCache(ttl_seconds=20, monotonic=lambda: now[0])
    authorizer, client = _authorizer(config, cache=cache)
    report = _report(config)

    initial = await authorizer.authorize(report, "@alice:localhost")
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember(report.publisher_matrix_user_id or "", "", "")],
        "!origin:localhost",
    )
    cached = await authorizer.authorize(report, "@alice:localhost")
    now[0] = 121.0
    after_expiry = await authorizer.authorize(report, "@alice:localhost")

    assert initial.authorized is True
    assert cached.authorized is True
    assert cached.cache_hit is True
    assert after_expiry.reason is ReportAuthorizationReason.VIEWER_NOT_JOINED


@pytest.mark.asyncio
async def test_success_cache_keys_isolate_security_identities(tmp_path: Path) -> None:
    """Room, viewer, and publisher identity changes must not share cached authority."""
    config = _config(tmp_path)
    authorizer, client = _authorizer(
        config,
        members={
            _report(config).publisher_matrix_user_id or "",
            "@alice:localhost",
            "@bob:localhost",
        },
    )
    report = _report(config)

    await authorizer.authorize(report, "@alice:localhost")
    await authorizer.authorize(report, "@bob:localhost")
    wrong_room = await authorizer.authorize(
        replace(report, origin_room_id="!other:localhost"),
        "@alice:localhost",
    )

    assert client.joined_members.await_count == 2
    assert wrong_room.reason is ReportAuthorizationReason.PUBLISHER_NOT_JOINED


@pytest.mark.asyncio
async def test_success_cache_coalesces_concurrent_checks() -> None:
    """Concurrent identical assets should share one in-flight membership check."""
    cache = SuccessfulReportAuthorizationCache()
    key = OriginRoomAuthorizationKey("!room:localhost", "@alice:localhost", "general", "@general:localhost")
    calls = 0

    async def check() -> ReportAuthorizationDecision:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZED)

    decisions = await asyncio.gather(*(cache.authorize(key, check) for _ in range(5)))

    assert calls == 1
    assert all(decision.authorized for decision in decisions)


@pytest.mark.asyncio
async def test_success_cache_is_bounded_and_never_caches_backend_errors() -> None:
    """Cache should evict old success and retry unavailable backends."""
    cache = SuccessfulReportAuthorizationCache(max_entries=2)
    calls = 0

    async def allowed() -> ReportAuthorizationDecision:
        return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZED)

    for index in range(3):
        await cache.authorize(
            OriginRoomAuthorizationKey(
                f"!room{index}:localhost",
                "@alice:localhost",
                "general",
                "@general:localhost",
            ),
            allowed,
        )

    error_key = OriginRoomAuthorizationKey(
        "!error:localhost",
        "@alice:localhost",
        "general",
        "@general:localhost",
    )

    async def unavailable() -> ReportAuthorizationDecision:
        nonlocal calls
        calls += 1
        return ReportAuthorizationDecision(ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE)

    await cache.authorize(error_key, unavailable)
    await cache.authorize(error_key, unavailable)

    assert cache.size == 2
    assert calls == 2
