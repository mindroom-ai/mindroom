"""Backend settings for boolean and choice judgments."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.schema_hints import dashboard_hint
from mindroom.credentials import validate_service_name


class LLMJudgmentConfig(BaseModel):
    """Use a configured model alias for a structured judgment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["llm"] = Field(description="Judge with a configured language model")
    model: str = Field(
        min_length=1,
        description="Configured model alias that answers the judgment",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        le=30.0,
        allow_inf_nan=False,
        description="Seconds to wait for the judgment before falling back",
    )


class _ProbabilityBackendConfig(BaseModel):
    """Accept a decision API's answer only at a task-specific probability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Minimum probability required to accept an answer",
    )
    timeout_seconds: float = Field(
        default=1.5,
        gt=0.0,
        le=30.0,
        allow_inf_nan=False,
        description="Seconds to wait for the judgment before falling back",
    )


class TypeSafeJudgmentConfig(_ProbabilityBackendConfig):
    """Use System One probabilities."""

    provider: Literal["typesafe"] = Field(description="Judge with System One; requires TYPESAFE_API_KEY")


class OpenAIDecisionsJudgmentConfig(_ProbabilityBackendConfig):
    """Use OpenAI Decisions API probabilities."""

    provider: Literal["openai_decisions"] = Field(
        description="Judge with the OpenAI Decisions API; uses the OpenAI API key",
    )
    credentials_service: str | None = Field(
        default=None,
        description=(
            "Credential service holding the OpenAI API key; defaults to the OpenAI provider credential, "
            "which may hold a proxy key that api.openai.com rejects"
        ),
    )

    @field_validator("credentials_service")
    @classmethod
    def _validate_credentials_service(cls, value: str | None) -> str | None:
        """Normalize an optional named credential reference."""
        return None if value is None else validate_service_name(value)


type ProbabilityJudgmentConfig = Annotated[
    TypeSafeJudgmentConfig | OpenAIDecisionsJudgmentConfig,
    Field(discriminator="provider"),
]
type JudgmentConfig = Annotated[
    LLMJudgmentConfig | TypeSafeJudgmentConfig | OpenAIDecisionsJudgmentConfig,
    Field(discriminator="provider"),
]
