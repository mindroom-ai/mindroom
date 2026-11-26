"""Bitbucket repository, commit, pull request, and issue toolkit."""

from __future__ import annotations

import base64
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

_ReadAction = Literal[
    "list_repositories",
    "repository_details",
    "commits",
    "pull_requests",
    "pull_request_details",
    "pull_request_changes",
    "issues",
]
_PrState = Literal["OPEN", "MERGED", "DECLINED", "SUPERSEDED"]


def _normalize_base_url(server_url: str, api_version: str) -> str:
    raw = require_non_empty(server_url, "Bitbucket server_url").rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        msg = "Invalid Bitbucket server_url."
        raise ValueError(msg)
    safe_version = safe_path_segment(require_non_empty(api_version, "api_version"), "api_version")
    return f"{raw}/{safe_version}"


class BitbucketTools(Toolkit):
    """Manage Bitbucket repositories, commits, pull requests, and issues."""

    def __init__(
        self,
        server_url: str = "api.bitbucket.org",
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        workspace: str | None = None,
        repo_slug: str | None = None,
        api_version: str = "2.0",
        timeout: int = 30,
    ) -> None:
        self.username = require_non_empty(username, "Bitbucket username")
        self.password = password.strip() if isinstance(password, str) and password.strip() else None
        self.token = token.strip() if isinstance(token, str) and token.strip() else None
        self.auth_password = self.token or self.password
        if not self.auth_password:
            msg = "Bitbucket token or password is required."
            raise ValueError(msg)
        self.server_url = server_url or "api.bitbucket.org"
        self.api_version = api_version or "2.0"
        self.base_url = _normalize_base_url(self.server_url, self.api_version)
        self.workspace = safe_path_segment(require_non_empty(workspace, "Bitbucket workspace"), "workspace")
        self.repo_slug = safe_path_segment(require_non_empty(repo_slug, "Bitbucket repo_slug"), "repo_slug")
        self.timeout = timeout
        encoded = base64.b64encode(f"{self.username}:{self.auth_password}".encode()).decode("ascii")
        self.headers = {"Accept": "application/json", "Authorization": f"Basic {encoded}"}
        super().__init__(name="bitbucket", tools=[self.bitbucket_read, self.bitbucket_create_repository])

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | str:
        url = f"{self.base_url}{endpoint}"
        response = httpx.request(
            method,
            url,
            headers=self.headers,
            params=params,
            json=data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "application/json")
        if content_type.startswith("application/json"):
            return response.json() if response.text else {}
        if content_type.startswith("text/plain"):
            return response.text
        return {}

    def _list_commits(self, count: int) -> dict[str, Any] | str:
        capped = max(1, min(count, 50))
        endpoint = f"/repositories/{self.workspace}/{self.repo_slug}/commits"
        commits = self._request("GET", endpoint, params={"pagelen": capped})
        if not isinstance(commits, dict) or not commits.get("next"):
            return commits
        collected = list(commits.get("values", []))
        current: dict[str, Any] = commits
        while len(collected) < capped and isinstance(current.get("next"), str):
            next_query = dict(parse_qsl(urlsplit(current["next"]).query))
            if not next_query:
                break
            page = self._request("GET", endpoint, params=next_query)
            if not isinstance(page, dict):
                break
            current = page
            collected.extend(page.get("values", []))
        current["values"] = collected[:capped]
        return current

    @staticmethod
    def _require_pr_id(pull_request_id: int | None) -> int:
        if pull_request_id is None or pull_request_id <= 0:
            msg = "pull_request_id must be a positive integer."
            raise ValueError(msg)
        return int(pull_request_id)

    def bitbucket_read(
        self,
        action: _ReadAction,
        count: int = 10,
        state: _PrState = "OPEN",
        pull_request_id: int | None = None,
    ) -> str:
        """Read Bitbucket repositories, commits, pull requests, diffs, and issues.

        Actions:
        - `list_repositories`: List workspace repositories (up to `count`, max 50).
        - `repository_details`: Fetch metadata for the configured repository.
        - `commits`: List repository commits (up to `count`, max 50).
        - `pull_requests`: List pull requests filtered by `state`.
        - `pull_request_details`: Fetch details for `pull_request_id`.
        - `pull_request_changes`: Fetch raw diff for `pull_request_id`.
        - `issues`: List repository issues (up to `count`, max 50).
        """
        try:
            capped = max(1, min(count, 50))
            repo_path = f"/repositories/{self.workspace}/{self.repo_slug}"
            if action == "list_repositories":
                result: dict[str, Any] | str = self._request(
                    "GET",
                    f"/repositories/{self.workspace}",
                    params={"page": 1, "pagelen": capped},
                )
            elif action == "repository_details":
                result = self._request("GET", repo_path)
            elif action == "commits":
                result = self._list_commits(capped)
            elif action == "pull_requests":
                result = self._request("GET", f"{repo_path}/pullrequests", params={"state": state})
            elif action == "pull_request_details":
                result = self._request("GET", f"{repo_path}/pullrequests/{self._require_pr_id(pull_request_id)}")
            elif action == "pull_request_changes":
                diff = self._request("GET", f"{repo_path}/pullrequests/{self._require_pr_id(pull_request_id)}/diff")
                return diff if isinstance(diff, str) else json_dumps(diff)
            else:
                result = self._request("GET", f"{repo_path}/issues", params={"pagelen": capped})
            return json_dumps(result)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))

    def bitbucket_create_repository(
        self,
        name: str,
        project: str | None = None,
        is_private: bool = False,
        description: str | None = None,
        language: str | None = None,
        has_issues: bool = False,
        has_wiki: bool = False,
    ) -> str:
        """Create a new Git repository in the configured Bitbucket workspace."""
        try:
            payload: dict[str, Any] = {
                "name": require_non_empty(name, "name"),
                "scm": "git",
                "is_private": is_private,
                "description": description,
                "language": language,
                "has_issues": has_issues,
                "has_wiki": has_wiki,
            }
            if project:
                payload["project"] = {"key": project}
            repo = self._request("POST", f"/repositories/{self.workspace}/{self.repo_slug}", data=payload)
            return json_dumps(repo)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="bitbucket",
    file_access=ToolFileAccess.NONE,
    display_name="Bitbucket",
    description="Manage Bitbucket repositories, pull requests, commits, and issues",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="SiBitbucket",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="username",
            env=("BITBUCKET_USERNAME",),
            label="Username",
            type="text",
            required=True,
            placeholder="you@example.com",
            description="Atlassian account email for Bitbucket Cloud; Basic-auth username for other deployments",
        ),
        ConfigField(
            name="password",
            env=("BITBUCKET_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            description="Basic-auth password for deployments that support it; use token for Bitbucket Cloud.",
        ),
        ConfigField(
            name="token",
            env=("BITBUCKET_TOKEN",),
            label="Token",
            type="password",
            required=False,
            description="Scoped Bitbucket Cloud API token; takes precedence over password.",
        ),
        ConfigField(
            name="workspace",
            label="Workspace",
            type="text",
            required=True,
            placeholder="my-workspace",
        ),
        ConfigField(
            name="repo_slug",
            label="Repository Slug",
            type="text",
            required=True,
            placeholder="my-repo",
        ),
        ConfigField(
            name="server_url",
            label="Server URL",
            type="url",
            required=False,
            default="api.bitbucket.org",
        ),
        ConfigField(
            name="api_version",
            label="API Version",
            type="text",
            required=False,
            default="2.0",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/project-management/#bitbucket",
    helper_text=(
        "For Bitbucket Cloud, [create a scoped API token]"
        "(https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/) "
        "and store it in Token, with your Atlassian account email in Username. "
        "Other deployments must accept the supplied credentials through Basic authentication."
    ),
    function_names=("bitbucket_read", "bitbucket_create_repository"),
    action_scoped_grants=("bitbucket_read",),
)
def bitbucket_tools() -> type[BitbucketTools]:
    """Return Bitbucket tools for repository management."""
    return BitbucketTools
