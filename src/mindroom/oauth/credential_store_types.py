"""Shared scope contract for OAuth storage and its compatibility boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class OAuthCredentialStoreContext(Protocol):
    """Fields the store needs from the lifecycle's canonical scope."""

    @property
    def runtime_paths(self) -> RuntimePaths:
        """Canonical runtime paths owning the credential scope."""
        ...

    @property
    def provider(self) -> OAuthProvider:
        """Provider whose credentials the scope stores."""
        ...

    @property
    def credentials_manager(self) -> CredentialsManager:
        """Credential codec and storage owner for the scope."""
        ...

    @property
    def worker_target(self) -> ResolvedWorkerTarget | None:
        """Canonical worker target, or an unscoped runtime."""
        ...
