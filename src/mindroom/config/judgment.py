"""Backend settings for boolean judgments and System One choices."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.schema_hints import dashboard_hint


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


class TypeSafeJudgmentConfig(BaseModel):
    """Use System One probabilities with a task-specific acceptance threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["typesafe"] = Field(description="Judge with System One; requires TYPESAFE_API_KEY")
    threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Minimum System One probability required to accept an answer",
    )
    timeout_seconds: float = Field(
        default=1.5,
        gt=0.0,
        le=30.0,
        allow_inf_nan=False,
        description="Seconds to wait for the judgment before falling back",
    )


type JudgmentConfig = Annotated[LLMJudgmentConfig | TypeSafeJudgmentConfig, Field(discriminator="provider")]
