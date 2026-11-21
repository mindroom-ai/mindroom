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
    warn_if_unavailable: bool,
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
    if not api_key:
        if warn_if_unavailable:
            logger.warning(
                "call_join_skipped_no_speech_key",
                room_id=room_id,
                agent=agent_name,
                component=component,
                provider=service.provider,
                credentials_service=service.credentials_service,
            )
        return None
    return SpeechServiceOptions(
        provider=service.provider,
        model=service.model,
        api_key=api_key,
        base_url=base_url,
        extra_kwargs=dict(service.extra_kwargs),
    )


@dataclass(frozen=True)
class MultiSfuFocusRefusal:
    """Devices whose publish transports disqualify the roster, not the logical call."""

    devices: frozenset[tuple[str, str]]


async def resolve_livekit_service(
    members: list[CallMember],
    *,
    user_id: str,
    configured_url: str | None,
    ssl_verify: bool,
) -> str | MultiSfuFocusRefusal | None:
    """Resolve the trusted service, or distinguish a refused multi-SFU roster from no service."""
    oldest_member = min(members, key=lambda member: member.created_ts)
    advertised_focus = _normalized_service_url(oldest_member.livekit_service_url)
    if advertised_focus is None or (configured_url is None and not advertised_focus.startswith("https://")):
        return multi_sfu_focus_refusal(members, None) if oldest_member.uses_multi_sfu else None
    local_server_name = MatrixID.parse(user_id).domain
    trusted_url = configured_url
    if trusted_url is None:
        trusted_url = await discover_livekit_service_url(
            local_server_name,
            ssl_verify=ssl_verify,
            allow_private_networks=True,
        )
        if (
            trusted_url is not None
            and _normalized_service_url(trusted_url) != advertised_focus
            and not any(member.uses_multi_sfu for member in members)
        ):
            # Preserve the legacy refresh. Multi-SFU rosters can be rechecked repeatedly, so reuse the cache.
            trusted_url = await discover_livekit_service_url(
                local_server_name,
                ssl_verify=ssl_verify,
                allow_private_networks=True,
                max_age_s=0,
            )
    trusted_focus = _normalized_service_url(trusted_url)
    if refusal := multi_sfu_focus_refusal(members, trusted_focus):
        return refusal
    if advertised_focus != trusted_focus:
        _log_untrusted_focus(oldest_member)
        return None
    return advertised_focus


def multi_sfu_focus_refusal(members: list[CallMember], service_url: str | None) -> MultiSfuFocusRefusal | None:
    """Report publishers that do not use the normalized, trusted service URL."""
    devices = set()
    for member in members:
        if member.uses_multi_sfu and (
            service_url is None or _normalized_service_url(member.livekit_service_url) != service_url
        ):
            _log_untrusted_focus(member)
            devices.add((member.user_id, member.device_id))
    return MultiSfuFocusRefusal(frozenset(devices)) if devices else None


def _log_untrusted_focus(member: CallMember) -> None:
    logger.warning(
        "call_focus_not_trusted",
        user_id=member.user_id,
        device_id=member.device_id,
        advertised_url=member.livekit_service_url,
    )


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


def _normalized_service_url(url: str | None) -> str | None:
    """Normalize insignificant URL spelling differences for focus comparison."""
    if url is None:
        return None
    try:
        parsed = httpx.URL(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            return None
        # HTTPX's default-port lookup uses the original, possibly uppercase scheme.
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port == default_port:
            parsed = parsed.copy_with(port=None)
        return str(parsed).rstrip("/")
    except (httpx.InvalidURL, UnicodeError):
        # Host access can raise on malformed IDNA even when URL construction succeeds.
        return None
