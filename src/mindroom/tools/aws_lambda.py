"""AWS Lambda serverless function listing and invocation toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.runtime_env_policy import AWS_ENV_BY_KEY
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import UNSET, UnsetType, require_non_empty, resolve_toggle

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _build_lambda_client(region_name: str, runtime_paths: RuntimePaths | None) -> Any:  # noqa: ANN401
    import boto3.session

    session_kwargs: dict[str, Any] = {"region_name": region_name}
    if runtime_paths is not None:
        access_key = runtime_paths.env_value(AWS_ENV_BY_KEY["access_key"])
        secret_key = runtime_paths.env_value(AWS_ENV_BY_KEY["secret_key"])
        session_token = runtime_paths.env_value(AWS_ENV_BY_KEY["session_token"])
        profile = runtime_paths.env_value(AWS_ENV_BY_KEY["profile"])
        if access_key or secret_key:
            if access_key:
                session_kwargs["aws_access_key_id"] = access_key
            if secret_key:
                session_kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                session_kwargs["aws_session_token"] = session_token
        elif profile:
            session_kwargs["profile_name"] = profile
    session = boto3.session.Session(**session_kwargs)
    return session.client("lambda", region_name=region_name)


class _AWSLambdaTools(Toolkit):
    """List and invoke AWS Lambda functions in a configured region."""

    def __init__(
        self,
        region_name: str = "us-east-1",
        enable_list_functions: bool | None | UnsetType = UNSET,
        enable_invoke_function: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
        *,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        self.region_name = require_non_empty(region_name, "AWS region_name")
        self._runtime_paths = runtime_paths
        self.client: Any = _build_lambda_client(self.region_name, runtime_paths)

        enable_all = resolve_toggle(all, default=False)
        tools: list[Any] = []
        if resolve_toggle(enable_list_functions, default=True, enable_all=enable_all):
            tools.append(self.aws_lambda_list_functions)
        if resolve_toggle(enable_invoke_function, default=True, enable_all=enable_all):
            tools.append(self.aws_lambda_invoke_function)
        super().__init__(name="aws_lambda", tools=tools)

    def aws_lambda_list_functions(self) -> str:
        """List AWS Lambda function names in the configured region."""
        try:
            response = self.client.list_functions()
            functions = [func["FunctionName"] for func in response.get("Functions", [])]
            return f"Available Lambda functions: {', '.join(functions)}"
        except Exception as exc:
            return f"Error listing functions: {exc}"

    def aws_lambda_invoke_function(self, function_name: str, payload: str = "{}") -> str:
        """Invoke AWS Lambda `function_name` with a JSON `payload` string."""
        try:
            clean_name = require_non_empty(function_name, "function_name")
            json.loads(payload)
            response = self.client.invoke(FunctionName=clean_name, Payload=payload)
            raw_payload = response["Payload"].read()
            decoded = raw_payload.decode("utf-8") if isinstance(raw_payload, bytes) else str(raw_payload)
            return f"Function invoked successfully. Status code: {response['StatusCode']}, Payload: {decoded}"
        except Exception as exc:
            return f"Error invoking function: {exc}"


@register_tool_with_metadata(
    name="aws_lambda",
    file_access=ToolFileAccess.NONE,
    display_name="AWS Lambda",
    description="Serverless function management and execution",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaAws",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="region_name",
            label="Region Name",
            type="text",
            required=False,
            default="us-east-1",
        ),
        bool_config_field("enable_list_functions", "Enable List Functions"),
        bool_config_field("enable_invoke_function", "Enable Invoke Function"),
        bool_config_field("all", "All", default=False),
    ],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    dependencies=["boto3"],
    docs_url="https://docs.mindroom.chat/tools/automation-and-platforms/#aws_lambda",
    function_names=("aws_lambda_invoke_function", "aws_lambda_list_functions"),
)
def aws_lambda_tools() -> type[Toolkit]:
    """Return AWS Lambda tools for serverless function management."""
    return _AWSLambdaTools
