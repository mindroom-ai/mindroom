"""Interactive Cloudflare Access headers for the local desktop Matrix client."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


_ACCESS_HEADER = "cf-access-token"
_TOKEN_TIMEOUT_SECONDS = 30


class CloudflareAccessError(RuntimeError):
    """Interactive Cloudflare Access authentication failed."""


class _TokenProvider(Protocol):
    def current_token(self) -> str | None:
        """Return the cached token when it remains safe to send."""
        ...

    def token(self) -> str:
        """Refresh if needed, then return one current Access token."""
        ...


@dataclass(slots=True)
class CloudflareAccessTokenProvider:
    """Cache one user Access JWT and renew it through cloudflared after expiry."""

    app_url: str
    executable: str
    clock: Callable[[], float] = time.time
    _token: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0, init=False, repr=False)
    _refresh_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def create(cls, app_url: str) -> CloudflareAccessTokenProvider:
        """Create a provider or report how to install its required CLI."""
        executable = shutil.which("cloudflared")
        if executable is None:
            msg = (
                "--cloudflare-access requires the cloudflared CLI. Install cloudflared, then rerun this command. "
                "See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/."
            )
            raise CloudflareAccessError(msg)
        return cls(app_url=app_url.rstrip("/"), executable=executable)

    def token(self) -> str:
        """Return a current Access JWT, opening browser login only when needed."""
        with self._refresh_lock:
            current = self.current_token()
            if current is not None:
                return current

            token = self._read_token()
            if token is not None and self._remember_if_current(token):
                return token

            self._login()
            token = self._read_token(required=True)
            assert token is not None
            if not self._remember_if_current(token):
                msg = "cloudflared returned an expired Cloudflare Access token after login."
                raise CloudflareAccessError(msg)
            return token

    def current_token(self) -> str | None:
        """Return the cached token until its documented expiry."""
        if self._token is None or self._expires_at <= self.clock():
            return None
        return self._token

    def _remember_if_current(self, token: str) -> bool:
        expires_at = _access_token_expiration(token)
        if expires_at <= self.clock():
            return False
        self._token = token
        self._expires_at = expires_at
        return True

    def _read_token(self, *, required: bool = False) -> str | None:
        args = [self.executable, "access", "token", f"-app={self.app_url}"]
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                check=False,
                text=True,
                timeout=_TOKEN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            msg = "cloudflared access token timed out."
            raise CloudflareAccessError(msg) from exc
        token = completed.stdout.strip()
        if completed.returncode == 0 and token:
            return token
        if not required:
            return None
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        msg = f"cloudflared access token failed after login: {detail}"
        raise CloudflareAccessError(msg)

    def _login(self) -> None:
        completed = subprocess.run(
            [self.executable, "access", "login", "--quiet", self.app_url],
            check=False,
        )
        if completed.returncode != 0:
            msg = f"cloudflared access login failed with exit {completed.returncode}."
            raise CloudflareAccessError(msg)


class CloudflareAccessHeaders(Mapping[str, str]):
    """Resolve the Access JWT when nio copies headers for each request."""

    def __init__(
        self,
        provider: _TokenProvider,
        static_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._static_headers = dict(static_headers or {})
        if any(name.lower() == _ACCESS_HEADER for name in self._static_headers):
            msg = "--cloudflare-access cannot be combined with a cf-access-token header."
            raise CloudflareAccessError(msg)

    def __getitem__(self, name: str) -> str:
        """Return one static header or the current interactive token."""
        if name.lower() == _ACCESS_HEADER:
            token = self._provider.current_token()
            if token is None:
                msg = "Cloudflare Access headers were used before asynchronous token preparation."
                raise CloudflareAccessError(msg)
            return token
        return self._static_headers[name]

    async def prepare(self) -> None:
        """Refresh the token off the event loop before nio copies request headers."""
        if self._provider.current_token() is None:
            await asyncio.to_thread(self._provider.token)

    def __iter__(self) -> Iterator[str]:
        """Yield static header names followed by the Access header."""
        yield from self._static_headers
        yield _ACCESS_HEADER

    def __len__(self) -> int:
        """Return combined static and interactive header count."""
        return len(self._static_headers) + 1


def cloudflare_access_headers(
    app_url: str,
    static_headers: Mapping[str, str] | None = None,
) -> CloudflareAccessHeaders:
    """Return request-time Cloudflare Access headers for one application."""
    return CloudflareAccessHeaders(CloudflareAccessTokenProvider.create(app_url), static_headers)


def _access_token_expiration(token: str) -> float:
    """Read the documented JWT expiry for refresh scheduling, not authorization."""
    try:
        encoded_payload = token.split(".")[1]
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
        expires_at = payload["exp"]
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        msg = "cloudflared returned a malformed Cloudflare Access token."
        raise CloudflareAccessError(msg) from exc
    if not isinstance(expires_at, int | float) or isinstance(expires_at, bool):
        msg = "cloudflared returned a Cloudflare Access token without a numeric expiry."
        raise CloudflareAccessError(msg)
    return float(expires_at)


__all__ = [
    "CloudflareAccessError",
    "CloudflareAccessHeaders",
    "CloudflareAccessTokenProvider",
    "cloudflare_access_headers",
]
