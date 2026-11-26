"""Linear issue tracking and project management toolkit."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty

_ReadAction = Literal["viewer", "teams", "issue", "user_issues", "workflow_issues", "high_priority"]
_WriteAction = Literal["create_issue", "update_issue"]

_LINEAR_ENDPOINT = "https://api.linear.app/graphql"

_QUERY_VIEWER = "query Me { viewer { id name email } }"
_QUERY_TEAMS = "query Teams { teams { nodes { id name } } }"
_QUERY_ISSUE = """
query IssueDetails($issueId: String!) {
  issue(id: $issueId) { id title description priority url state { id name } }
}
"""
_QUERY_USER_ISSUES = """
query UserAssignedIssues($userId: String!) {
  user(id: $userId) { id name assignedIssues { nodes { id title } } }
}
"""
_QUERY_WORKFLOW_ISSUES = """
query WorkflowStateIssues($workflowId: String!) {
  workflowState(id: $workflowId) { issues { nodes { id title } } }
}
"""
_QUERY_HIGH_PRIORITY = """
query HighPriorityIssues {
  issues(filter: { priority: { lte: 2 } }) { nodes { id title priority } }
}
"""
_MUTATION_CREATE_ISSUE = """
mutation IssueCreate($title: String!, $description: String!, $teamId: String!, $projectId: String, $assigneeId: String) {
  issueCreate(input: { title: $title, description: $description, teamId: $teamId, projectId: $projectId, assigneeId: $assigneeId }) {
    success
    issue { id title url }
  }
}
"""
_MUTATION_UPDATE_ISSUE = """
mutation IssueUpdate($issueId: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $issueId, input: $input) {
    success
    issue { id title state { id name } }
  }
}
"""


class LinearTools(Toolkit):
    """Issue tracking and project management via Linear's GraphQL API."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = require_non_empty(api_key, "Linear API key")
        self.timeout = timeout
        self.endpoint = _LINEAR_ENDPOINT
        self.headers = {"Authorization": self.api_key}
        super().__init__(name="linear", tools=[self.linear_read, self.linear_write])

    def _execute_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        response = httpx.post(self.endpoint, json=payload, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            msg = "Unexpected non-object response from Linear GraphQL API."
            raise TypeError(msg)
        if body.get("errors"):
            msg = f"GraphQL Error: {body['errors']}"
            raise RuntimeError(msg)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _read_dispatch(
        self,
        action: _ReadAction,
        issue_id: str | None,
        user_id: str | None,
        workflow_id: str | None,
    ) -> Any:  # noqa: ANN401
        if action == "viewer":
            result: Any = self._execute_query(_QUERY_VIEWER).get("viewer")
        elif action == "teams":
            teams = self._execute_query(_QUERY_TEAMS).get("teams")
            result = teams.get("nodes", []) if isinstance(teams, dict) else []
        elif action == "issue":
            result = self._execute_query(_QUERY_ISSUE, {"issueId": require_non_empty(issue_id, "issue_id")}).get(
                "issue",
            )
        elif action == "user_issues":
            target = require_non_empty(user_id, "user_id")
            user = self._execute_query(_QUERY_USER_ISSUES, {"userId": target}).get("user")
            result = (
                user.get("assignedIssues", {}).get("nodes", [])
                if isinstance(user, dict)
                else {"error": f"User '{target}' not found."}
            )
        elif action == "workflow_issues":
            target = require_non_empty(workflow_id, "workflow_id")
            state = self._execute_query(_QUERY_WORKFLOW_ISSUES, {"workflowId": target}).get("workflowState")
            result = (
                state.get("issues", {}).get("nodes", [])
                if isinstance(state, dict)
                else {"error": f"Workflow state '{target}' not found."}
            )
        else:
            issues = self._execute_query(_QUERY_HIGH_PRIORITY).get("issues")
            result = issues.get("nodes", []) if isinstance(issues, dict) else []
        return result

    def linear_read(
        self,
        action: _ReadAction,
        issue_id: str | None = None,
        user_id: str | None = None,
        workflow_id: str | None = None,
    ) -> str:
        """Read Linear user, team, and issue data.

        Actions:
        - `viewer`: Authenticated user profile.
        - `teams`: Accessible teams.
        - `issue`: Details for `issue_id`.
        - `user_issues`: Issues assigned to `user_id`.
        - `workflow_issues`: Issues in workflow state `workflow_id`.
        - `high_priority`: Issues with priority <= 2.
        """
        try:
            return json_dumps(self._read_dispatch(action, issue_id, user_id, workflow_id))
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            return error_json(str(exc))

    def _create_issue(
        self,
        title: str | None,
        description: str | None,
        team_id: str | None,
        project_id: str | None,
        assignee_id: str | None,
    ) -> str:
        variables: dict[str, Any] = {
            "title": require_non_empty(title, "title"),
            "description": description or "",
            "teamId": require_non_empty(team_id, "team_id"),
        }
        if project_id is not None:
            variables["projectId"] = project_id
        if assignee_id is not None:
            variables["assigneeId"] = assignee_id
        created = self._execute_query(_MUTATION_CREATE_ISSUE, variables).get("issueCreate")
        if not isinstance(created, dict) or not created.get("success"):
            return error_json("Issue creation failed.")
        return json_dumps(created.get("issue"))

    def _update_issue(
        self,
        issue_id: str | None,
        title: str | None,
        description: str | None,
        assignee_id: str | None,
    ) -> str:
        target = require_non_empty(issue_id, "issue_id")
        update_input = {
            key: value
            for key, value in (("title", title), ("description", description), ("assigneeId", assignee_id))
            if value is not None
        }
        if not update_input:
            return error_json("Provide at least one field to update (title, description, or assignee_id).")
        updated = self._execute_query(_MUTATION_UPDATE_ISSUE, {"issueId": target, "input": update_input}).get(
            "issueUpdate",
        )
        if not isinstance(updated, dict) or not updated.get("success"):
            return error_json(f"Failed to update issue '{target}'.")
        return json_dumps(updated.get("issue"))

    def linear_write(
        self,
        action: _WriteAction,
        title: str | None = None,
        description: str | None = None,
        team_id: str | None = None,
        issue_id: str | None = None,
        project_id: str | None = None,
        assignee_id: str | None = None,
    ) -> str:
        """Create or update Linear issues.

        Actions:
        - `create_issue`: Create an issue (`title`, `team_id`; optional `description`, `project_id`, `assignee_id`).
        - `update_issue`: Update `issue_id` (`title`, `description`, or `assignee_id`).
        """
        try:
            if action == "create_issue":
                return self._create_issue(title, description, team_id, project_id, assignee_id)
            return self._update_issue(issue_id, title, description, assignee_id)
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="linear",
    file_access=ToolFileAccess.NONE,
    display_name="Linear",
    description="Issue tracking and project management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiLinear",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("LINEAR_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
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
    docs_url="https://docs.mindroom.chat/tools/project-management/#linear",
    function_names=("linear_read", "linear_write"),
    action_scoped_grants=("linear_read", "linear_write"),
)
def linear_tools() -> type[LinearTools]:
    """Return Linear tools for issue tracking and project management."""
    return LinearTools
