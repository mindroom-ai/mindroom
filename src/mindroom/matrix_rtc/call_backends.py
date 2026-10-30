"""Resolve a voice call's backend credentials, speech endpoints, and trusted LiveKit service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx
import nio

from mindroom.config.voice import normalize_speech_base_url
from mindroom.credentials.sync import get_api_key_for_service
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_rtc.call_session import CallJoinError, required_device_id
from mindroom.matrix_rtc.focus import OpenIDToken, discover_livekit_service_url, request_sfu_grant
from mindroom.matrix_rtc.voice_agent import SpeechServiceOptions
from mindroom.model_defaults import LOCAL_OPENAI_API_KEY_DEFAULT, OPENAI_BASE_URL_DEFAULT

if TYPE_CHECKING:
    from mindroom.config.calls import CallProfile, CascadedCallProfile, LiveCallProfile, RealtimeCallProfile
    from mindroom.config.voice import SpeechServiceConfig
    from mindroom.constants import RuntimePaths
    from mindroom.matrix_rtc.events import CallMember
    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedVoiceBackend:
    """Credentials and endpoints resolved for one configured call backend."""

    realtime_api_key: str | None = None
    stt: SpeechServiceOptions | None = None
    tts: SpeechServiceOptions | None = None


def resolve_voice_backend(
    call_config: CallProfile,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    room_id: str | None,
    warn_if_unavailable: bool = True,
) -> ResolvedVoiceBackend | None:
    """Resolve backend-specific credentials without affecting call lifecycle."""
    if call_config.backend in {"realtime", "live"}:
        realtime_config = cast("RealtimeCallProfile | LiveCallProfile", call_config)
        api_key = get_api_key_for_service(realtime_config.credentials_service, runtime_paths)
        if not api_key:
            if warn_if_unavailable:
                logger.warning(
                    "call_join_skipped_no_openai_key",
                    room_id=room_id,
                    agent=agent_name,
                    credentials_service=realtime_config.credentials_service,
                )
            return None
        return ResolvedVoiceBackend(realtime_api_key=api_key)

    cascaded_config = cast("CascadedCallProfile", call_config)
    stt = _resolve_speech_service(
        cascaded_config.stt,
        runtime_paths,
        agent_name=agent_name,
        component="stt",
        room_id=room_id,
        warn_if_unavailable=warn_if_unavailable,
    )
    tts = _resolve_speech_service(
        cascaded_config.tts,
        runtime_paths,
        agent_name=agent_name,
        component="tts",
        room_id=room_id,
        warn_if_unavailable=warn_if_unavailable,
    )
    if stt is None or tts is None:
        return None
    return ResolvedVoiceBackend(stt=stt, tts=tts)


def _resolve_speech_service(
    service: SpeechServiceConfig,
    runtime_paths: RuntimePaths,
    *,
    agent_name: str,
    component: str,
    room_id: str | None,
    warn_if_unavailable: bool = True,
) -> SpeechServiceOptions | None:
    """Resolve one independently credentialed cloud or local speech service."""
    base_url = normalize_speech_base_url(service.host)
    if base_url is None and service.provider == "openai":
        base_url = OPENAI_BASE_URL_DEFAULT
    api_key = service.api_key
    if api_key is None:
        if service.credentials_service is not None:
            api_key = get_api_key_for_service(service.credentials_service, runtime_paths)
        elif service.provider == "openai_compatible":
            api_key = LOCAL_OPENAI_API_KEY_DEFAULT
    if not api_key and warn_if_unavailable:
        logger.warning(
            "call_join_skipped_no_speech_key",
            room_id=room_id,
            agent=agent_name,
            component=component,
            provider=service.provider,
            credentials_service=service.credentials_service,
        )
    if not api_key:
        return None
    return SpeechServiceOptions(
        provider=service.provider,
        model=service.model,
        api_key=api_key,
        base_url=base_url,
        extra_kwargs=dict(service.extra_kwargs),
    )


async def resolve_livekit_service(
    members: list[CallMember],
    *,
    user_id: str,
    configured_url: str | None,
    ssl_verify: bool,
) -> str | None:
    """Accept only the locally configured or discovered authorization service."""
    oldest_member = min(members, key=lambda member: member.created_ts)
    advertised_url = oldest_member.livekit_service_url
    if advertised_url is None:
        return None
    advertised_focus = _normalized_service_url(advertised_url)
    if advertised_focus is None:
        return None
    local_server_name = MatrixID.parse(user_id).domain
    trusted_url = configured_url
    if trusted_url is None:
        if not advertised_focus.startswith("https://"):
            return None
        trusted_url = await discover_livekit_service_url(
            local_server_name,
            ssl_verify=ssl_verify,
            allow_private_networks=True,
        )
    trusted_focus = _normalized_service_url(trusted_url) if trusted_url is not None else None
    if advertised_focus != trusted_focus:
        logger.warning(
            "call_focus_not_trusted",
            user_id=oldest_member.user_id,
            advertised_url=advertised_url,
        )
        return None
    return advertised_focus


async def fetch_sfu_grant(client: nio.AsyncClient, room_id: str, service_url: str, *, ssl_verify: bool) -> SfuGrant:
    """Exchange the bot's OpenID token for a grant to join the room's call on the SFU."""
    response = await client.get_openid_token(client.user_id)
    if isinstance(response, nio.responses.GetOpenIDTokenError):
        msg = f"OpenID token request failed: {response.message}"
        raise CallJoinError(msg)
    openid_token = OpenIDToken(
        access_token=response.access_token,
        expires_in=response.expires_in,
        matrix_server_name=response.matrix_server_name,
        token_type=response.token_type,
    )
    return await request_sfu_grant(
        service_url,
        room_id=room_id,
        device_id=required_device_id(client),
        openid_token=openid_token,
        ssl_verify=ssl_verify,
        allow_private_networks=True,
    )


def _normalized_service_url(url: str) -> str | None:
    """Normalize insignificant URL spelling differences for focus comparison."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.host is None
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        return None
    return str(parsed).rstrip("/")
