"""Private Agno Google authentication bindings, which only toolkits subclassing Agno's Google toolkits mix in.

MindRoom supplies credential resolution and entrypoint policy callbacks. This
module owns only the private resolver name and Function entrypoint mutation
required by Agno's Google toolkits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


# AGNO_COMPAT: Google authentication lacks injectable credentials and entrypoint hooks.
# Reason: Agno's Google authentication decorator calls the private
# ``_resolve_creds`` method directly, and registered Function entrypoints do not
# expose a public authentication/error-policy middleware hook. Only the toolkits
# that subclass Agno's Gmail, Calendar, Drive, and Sheets toolkits mix this in;
# MindRoom's own Google toolkits wrap their functions through the OAuth client.
# Upstream issue: No matching public Google credential-provider hook identified.
# Upstream PR: None identified; the missing extension point remains untracked.
# Remove when: Agno accepts an injected credential resolver and entrypoint
# middleware while retaining owner-controlled authentication and result policy.
# Coverage: tests/test_google_tool_wrappers.py::test_agno_resolve_creds_routes_through_mindroom_auth;
# tests/test_google_tool_wrappers.py::test_google_wrapper_supplied_credentials_lock_is_reentrant_and_serializes_workers.


class AgnoGoogleAuthBindingMixin:
    """Adapt Agno's private Google resolver and registered Function entrypoints."""

    functions: dict[str, Any]
    creds: Any | None

    _authenticate: Callable[[], None]
    _oauth_entrypoint: Callable[[Callable[..., object]], Callable[..., object]]

    def _resolve_creds(self) -> Any:  # noqa: ANN401
        """Route Agno's private resolver call through the owner's auth policy."""
        self._authenticate()
        return self.creds

    def _wrap_oauth_function_entrypoints(self) -> None:
        """Route each registered Agno Function, and a direct call of its method, through the owner's call policy."""
        for function in self.functions.values():
            if function.entrypoint is None:
                continue
            function.entrypoint = self._oauth_entrypoint(function.entrypoint)
            setattr(self, function.name, function.entrypoint)
