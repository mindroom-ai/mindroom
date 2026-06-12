"""Tests for dashboard credential scope resolution and authorization checks."""

from typing import Any, ClassVar

import pytest
from fastapi import HTTPException, Request

from mindroom.api.dashboard_credential_scope import (
    require_agent_credential_management_authorized,
    resolve_dashboard_agent_execution_scope_request,
    resolve_dashboard_execution_scope_override,
)
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths


def _request(auth_user: dict[str, Any] | None = None, query_string: bytes = b"") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": query_string,
    }
    if auth_user is not None:
        scope["auth_user"] = auth_user
    return Request(scope)


def _config(worker_scope: str | None = None, authorization: dict[str, object] | None = None) -> Config:
    payload: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        "agents": {
            "general": {
                "display_name": "General",
                "role": "test",
                "tools": ["calculator"],
                "instructions": ["hi"],
                "rooms": ["lobby"],
            },
        },
    }
    if authorization is not None:
        payload["authorization"] = authorization
    config = Config.model_validate(payload)
    config.agents["general"].worker_scope = worker_scope
    return config


class TestResolveDashboardExecutionScopeOverride:
    """Accept/reject matrix for the execution_scope query parameter."""

    def test_absent_parameter_is_not_an_override(self) -> None:
        """A missing execution_scope parameter is not an override."""
        assert resolve_dashboard_execution_scope_override(_request()) == (False, None)

    def test_empty_parameter_is_not_an_override(self) -> None:
        """An empty execution_scope parameter is not an override."""
        assert resolve_dashboard_execution_scope_override(_request(query_string=b"execution_scope=")) == (False, None)

    def test_unscoped_is_an_explicit_none_override(self) -> None:
        """execution_scope=unscoped is an explicit override to no scope."""
        assert resolve_dashboard_execution_scope_override(_request(query_string=b"execution_scope=unscoped")) == (
            True,
            None,
        )

    @pytest.mark.parametrize("scope", ["shared", "user", "user_agent"])
    def test_known_scopes_are_explicit_overrides(self, scope: str) -> None:
        """Known scope values are explicit overrides."""
        request = _request(query_string=f"execution_scope={scope}".encode())
        assert resolve_dashboard_execution_scope_override(request) == (True, scope)

    def test_unknown_scope_is_rejected(self) -> None:
        """Unknown scope values are rejected with 400."""
        with pytest.raises(HTTPException) as exc_info:
            resolve_dashboard_execution_scope_override(_request(query_string=b"execution_scope=galactic"))
        assert exc_info.value.status_code == 400


class TestResolveDashboardAgentExecutionScopeRequest:
    """Accept/reject matrix for agent execution-scope resolution."""

    def test_no_agent_and_no_override_resolves_unscoped(self) -> None:
        """No agent and no override resolves to the unscoped target."""
        resolution = resolve_dashboard_agent_execution_scope_request(
            config=_config(),
            agent_name=None,
            execution_scope_override_provided=False,
            execution_scope_override=None,
            allow_draft_override=False,
        )
        assert resolution.agent_name is None
        assert resolution.requested_execution_scope is None
        assert resolution.draft_scope_preview is False

    def test_override_without_agent_is_rejected(self) -> None:
        """An override without agent_name is rejected with 400."""
        with pytest.raises(HTTPException) as exc_info:
            resolve_dashboard_agent_execution_scope_request(
                config=_config(),
                agent_name=None,
                execution_scope_override_provided=True,
                execution_scope_override="shared",
                allow_draft_override=False,
            )
        assert exc_info.value.status_code == 400

    def test_unknown_agent_is_rejected(self) -> None:
        """An unknown agent is rejected with 404."""
        with pytest.raises(HTTPException) as exc_info:
            resolve_dashboard_agent_execution_scope_request(
                config=_config(),
                agent_name="nope",
                execution_scope_override_provided=False,
                execution_scope_override=None,
                allow_draft_override=False,
            )
        assert exc_info.value.status_code == 404

    def test_agent_without_override_uses_persisted_scope(self) -> None:
        """Without an override the persisted agent scope is used."""
        resolution = resolve_dashboard_agent_execution_scope_request(
            config=_config("shared"),
            agent_name="general",
            execution_scope_override_provided=False,
            execution_scope_override=None,
            allow_draft_override=False,
        )
        assert resolution.agent_name == "general"
        assert resolution.requested_execution_scope == "shared"
        assert resolution.draft_scope_preview is False

    def test_override_matching_persisted_scope_is_accepted(self) -> None:
        """An override equal to the persisted scope is accepted."""
        resolution = resolve_dashboard_agent_execution_scope_request(
            config=_config("shared"),
            agent_name="general",
            execution_scope_override_provided=True,
            execution_scope_override="shared",
            allow_draft_override=False,
        )
        assert resolution.requested_execution_scope == "shared"
        assert resolution.draft_scope_preview is False

    def test_draft_override_is_rejected_when_drafts_disallowed(self) -> None:
        """A draft override is rejected with 409 when drafts are disallowed."""
        with pytest.raises(HTTPException) as exc_info:
            resolve_dashboard_agent_execution_scope_request(
                config=_config(None),
                agent_name="general",
                execution_scope_override_provided=True,
                execution_scope_override="shared",
                allow_draft_override=False,
            )
        assert exc_info.value.status_code == 409
        assert "Save the configuration" in exc_info.value.detail

    def test_draft_override_is_accepted_when_drafts_allowed(self) -> None:
        """A draft override is accepted when drafts are allowed."""
        resolution = resolve_dashboard_agent_execution_scope_request(
            config=_config(None),
            agent_name="general",
            execution_scope_override_provided=True,
            execution_scope_override="shared",
            allow_draft_override=True,
        )
        assert resolution.requested_execution_scope == "shared"
        assert resolution.draft_scope_preview is True


class TestRequireAgentCredentialManagementAuthorized:
    """Accept/reject matrix for credential management per caller role."""

    _allowlist: ClassVar[dict[str, object]] = {"agent_reply_permissions": {"*": ["@alice:example.org"]}}

    def _runtime_paths(self) -> RuntimePaths:
        return resolve_runtime_paths(process_env={})

    def test_allowlisted_trusted_upstream_matrix_user_is_authorized(self) -> None:
        """Return isolated runtime paths with an empty process env."""
        identity = require_agent_credential_management_authorized(
            _request({"user_id": "alice", "auth_source": "trusted_upstream", "matrix_user_id": "@alice:example.org"}),
            config=_config(authorization=self._allowlist),
            runtime_paths=self._runtime_paths(),
            agent_name="general",
        )
        assert identity.requester_id == "@alice:example.org"
        assert identity.agent_name == "general"

    def test_non_allowlisted_trusted_upstream_matrix_user_is_rejected(self) -> None:
        """A non-allowlisted trusted-upstream Matrix user is rejected with 403."""
        with pytest.raises(HTTPException) as exc_info:
            require_agent_credential_management_authorized(
                _request(
                    {
                        "user_id": "mallory",
                        "auth_source": "trusted_upstream",
                        "matrix_user_id": "@mallory:example.org",
                    },
                ),
                config=_config(authorization=self._allowlist),
                runtime_paths=self._runtime_paths(),
                agent_name="general",
            )
        assert exc_info.value.status_code == 403

    def test_dashboard_user_without_matrix_identity_is_rejected_by_allowlist(self) -> None:
        """A dashboard user without a Matrix identity fails a Matrix-ID allowlist."""
        with pytest.raises(HTTPException) as exc_info:
            require_agent_credential_management_authorized(
                _request({"user_id": "alice"}),
                config=_config(authorization=self._allowlist),
                runtime_paths=self._runtime_paths(),
                agent_name="general",
            )
        assert exc_info.value.status_code == 403

    def test_dashboard_user_is_authorized_when_no_allowlist_is_configured(self) -> None:
        """Without an allowlist any authenticated dashboard user is authorized."""
        identity = require_agent_credential_management_authorized(
            _request({"user_id": "alice"}),
            config=_config(),
            runtime_paths=self._runtime_paths(),
            agent_name="general",
        )
        assert identity.requester_id == "alice"

    def test_unresolvable_requester_is_rejected(self) -> None:
        """Requests without a resolvable requester identity are rejected with 403."""
        with pytest.raises(HTTPException) as exc_info:
            require_agent_credential_management_authorized(
                _request({"auth_source": "trusted_upstream"}),
                config=_config(),
                runtime_paths=self._runtime_paths(),
                agent_name="general",
            )
        assert exc_info.value.status_code == 403
