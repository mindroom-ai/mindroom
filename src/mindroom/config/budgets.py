"""Opt-in per-user monthly spending budgets."""

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat

from mindroom.config.schema_hints import dashboard_hint


class BudgetsConfig(BaseModel):
    """Default monthly USD cap per requester, with a fallback model once a cap is reached."""

    model_config = ConfigDict(extra="forbid")

    monthly_limit_usd: NonNegativeFloat | None = Field(
        default=None,
        description="Default monthly cap in USD for each user without a cap of their own; unset leaves them uncapped",
    )
    fallback_model: str = Field(
        description="Model that replies use instead of a priced model once the requester reaches their cap",
        json_schema_extra=dashboard_hint(reference="model"),
    )
