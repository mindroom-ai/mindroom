"""The strict schema of a `ui` block; only `chart` is a widget so far."""

from __future__ import annotations

import json
from typing import Annotated, Literal, LiteralString

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

_MAX_BLOCK_BYTES = 16_384
_MAX_SERIES = 8
_MAX_POINTS = 200

_Title = Annotated[str, Field(min_length=1, max_length=80)]
_ShortLabel = Annotated[str, Field(min_length=1, max_length=40)]
_Number = Annotated[float, Field(allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Series(_Strict):
    """One named row of numbers, one per label."""

    name: _ShortLabel
    data: Annotated[list[_Number], Field(min_length=1, max_length=_MAX_POINTS)]


class Chart(_Strict):
    """A bar, line, area or pie chart."""

    type: Literal["chart"]
    kind: Literal["bar", "line", "area", "pie"]
    title: _Title
    labels: Annotated[list[_ShortLabel], Field(min_length=1, max_length=_MAX_POINTS)]
    series: Annotated[list[Series], Field(min_length=1, max_length=_MAX_SERIES)]
    x: _ShortLabel | None = None
    y: _ShortLabel | None = None

    @model_validator(mode="after")
    def _check(self) -> Chart:
        rule = _broken_rule(self)
        if rule is not None:
            raise PydanticCustomError(rule, rule)
        return self


def _broken_rule(chart: Chart) -> LiteralString | None:
    """The cross-field rule a chart breaks, named without any of the chart's text."""
    if any(len(series.data) != len(chart.labels) for series in chart.series):
        return "series_length"
    if len({series.name for series in chart.series}) != len(chart.series):
        return "series_names"
    if chart.kind == "pie":
        if len(chart.series) != 1:
            return "pie_series"
        data = chart.series[0].data
        if any(value < 0 for value in data) or not any(data):
            return "pie_values"
    return None


def _reject_constant(_name: str) -> float:
    msg = "not valid JSON"
    raise ValueError(msg)


def _label(value: object) -> object:
    """A numeric label as the text a person would write; strict mode takes no number for a string."""
    if type(value) is int:
        return str(value)
    if type(value) is float and value == value and abs(value) != float("inf"):  # noqa: PLR0124
        return str(int(value)) if value.is_integer() else repr(value)
    return value


def parse_widget(json_text: str) -> tuple[Chart | None, list[str]]:
    """Return the widget a block's text describes, or `None` and why; never raises, and no error holds input text."""
    if len(json_text.encode("utf-8", "surrogatepass")) > _MAX_BLOCK_BYTES:
        return None, [f"block is larger than {_MAX_BLOCK_BYTES} bytes"]
    try:
        raw = json.loads(json_text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        return None, [f"json: {type(error).__name__}"]
    if not isinstance(raw, dict):
        return None, ["json: top level must be an object"]
    if isinstance(raw.get("labels"), list):
        raw["labels"] = [_label(value) for value in raw["labels"]]
    try:
        return Chart.model_validate(raw), []
    except ValidationError as error:
        return None, [
            # The last location of an unknown key is the key itself, which is input text.
            ".".join(str(part) for part in (e["loc"][:-1] if e["type"] == "extra_forbidden" else e["loc"]))
            + f": {e['type']}"
            for e in error.errors(include_url=False, include_input=False, include_context=False)
        ]
