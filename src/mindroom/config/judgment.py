"""Backend settings for boolean judgments and System One choices."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class LLMJudgmentConfig(BaseModel):
    """Use a configured model alias for a structured judgment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["llm"]
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0, allow_inf_nan=False)


class TypeSafeJudgmentConfig(BaseModel):
    """Use System One probabilities with a task-specific acceptance threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["typesafe"]
    threshold: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    timeout_seconds: float = Field(default=1.5, gt=0.0, le=30.0, allow_inf_nan=False)


type JudgmentConfig = Annotated[LLMJudgmentConfig | TypeSafeJudgmentConfig, Field(discriminator="provider")]
