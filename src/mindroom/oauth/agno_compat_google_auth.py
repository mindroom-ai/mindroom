"""Private Agno Google authentication bindings.

MindRoom supplies credential resolution and entrypoint policy callbacks. This
module owns only the private resolver name and Function entrypoint mutation
required by Agno's Google toolkits.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


# AGNO_COMPAT: Google authentication lacks injectable credentials and entrypoint hooks.
# Reason: Agno's Google authentication decorator calls the private
# ``_resolve_creds`` method directly, and registered Function entrypoints do not
# expose a public authentication/error-policy middleware hook.
# Upstream issue: No matching public Google credential-provider hook identified.
# Upstream PR: None identified; the missing extension point remains untracked.
# Remove when: Agno accepts an injected credential resolver and entrypoint
# middleware while retaining owner-controlled authentication and result policy.
# Coverage: tests/test_google_tool_wrappers.py::test_agno_resolve_creds_routes_through_mindroom_auth;
# tests/test_google_tool_wrappers.py::test_google_wrapper_supplied_credentials_lock_is_reentrant_and_serializes_workers.


class _AuthDescriptor(Protocol):
    """Descriptor contract for an unbound toolkit authentication method."""

    def __get__(self, instance: object, owner: type[object] | None = None) -> Callable[[], Any]:
        """Bind the authentication method to one toolkit instance."""


class AgnoGoogleAuthBindingMixin:
    """Adapt Agno's private Google resolver and registered Function entrypoints."""

    functions: dict[str, Any]
    creds: Any | None
    _original_auth: Callable[[], Any]

    _authenticate: Callable[[], None]
    _run_oauth_entrypoint: Callable[[Callable[..., object], tuple[object, ...], dict[str, object]], object]

    def _set_original_auth(self, auth_method: _AuthDescriptor) -> None:
        """Retain the owner's bound fallback behind Agno's descriptor protocol."""
        self._original_auth = auth_method.__get__(self, type(self))

    def _resolve_creds(self) -> Any:  # noqa: ANN401
        """Route Agno's private resolver call through the owner's auth policy."""
        self._authenticate()
        return self.creds

    def _wrap_oauth_function_entrypoints(self) -> None:
        """Route each registered Agno Function through the owner's call policy."""
        for function in self.functions.values():
            entrypoint = function.entrypoint
            if entrypoint is None:
                continue

            @wraps(entrypoint)
            def oauth_entrypoint(
                *args: object,
                _entrypoint: Callable[..., object] = entrypoint,
                **kwargs: object,
            ) -> object:
                return self._run_oauth_entrypoint(_entrypoint, args, kwargs)

            function.entrypoint = oauth_entrypoint
            setattr(self, function.name, oauth_entrypoint)
