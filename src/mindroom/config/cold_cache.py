"""Opt-in switch to a smaller model once a thread's prompt cache has expired."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig, LLMJudgmentConfig
from mindroom.config.schema_hints import dashboard_hint


class ColdCacheConfig(BaseModel):
    """Let a judge move cold-cache turns to a cheaper model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(
        min_length=1,
        description="Smaller model alias used when the judgment accepts a cold-cache turn",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    judgment: JudgmentConfig | None = Field(
        default=None,
        description="LLM model alias, TypeSafe, or OpenAI Decisions backend; unset asks the smaller model itself",
    )

    def effective_judgment(self) -> JudgmentConfig:
        """Return the configured backend, defaulting to the smaller model as an LLM judge."""
        return self.judgment or LLMJudgmentConfig(provider="llm", model=self.model)
