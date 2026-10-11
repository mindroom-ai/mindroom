"""Opt-in per-user monthly spending budgets."""

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, field_validator

from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.config.schema_hints import dashboard_hint


class BudgetsConfig(BaseModel):
    """Monthly USD caps per requester, with a fallback model once a cap is reached."""

    model_config = ConfigDict(extra="forbid")

    monthly_limit_usd: NonNegativeFloat | None = Field(
        default=None,
        description="Default monthly cap in USD for each user; unset leaves users without an override uncapped",
    )
    fallback_model: str = Field(
        description="Model that replies use instead of a priced model once the requester reaches their cap",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    users: dict[str, NonNegativeFloat] = Field(
        default_factory=dict,
        description="Monthly USD caps for specific Matrix users, overriding monthly_limit_usd",
    )

    @field_validator("users")
    @classmethod
    def _validate_user_ids(cls, users: dict[str, float]) -> dict[str, float]:
        validate_concrete_matrix_user_ids(list(users), field_name="budgets.users")
        return users
