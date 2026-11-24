"""Opt-in switch to the agent's cheap model once a thread's prompt cache has expired."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig, LLMJudgmentConfig
from mindroom.constants import PROMPT_CACHE_IDLE_SECONDS


class ColdCacheConfig(BaseModel):
    """Let a judge move a thread to the agent's cheap model once its prompt cache went cold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idle_seconds: int = Field(
        default=PROMPT_CACHE_IDLE_SECONDS,
        ge=1,
        description="Seconds since the agent's last reply in a thread after which its prompt cache counts as cold",
    )
    judgment: JudgmentConfig | None = Field(
        default=None,
        description="LLM model alias, TypeSafe, or OpenAI Decisions backend; unset asks the cheap model itself",
    )

    def effective_judgment(self, cheap_model: str) -> JudgmentConfig:
        """Return the configured backend, defaulting to the cheap model as an LLM judge."""
        return self.judgment or LLMJudgmentConfig(provider="llm", model=cheap_model)
