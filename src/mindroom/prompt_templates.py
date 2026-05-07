"""Small prompt-template renderer and validation helpers."""

from __future__ import annotations

import re
from string import Formatter

from mindroom.prompts import PROMPT_TEMPLATE_FIELDS

__all__ = [
    "PromptTemplateError",
    "build_agent_identity_context",
    "render_prompt_template",
    "validate_prompt_template_fields",
]

_PROMPT_PLACEHOLDER_RE = re.compile(r"(?<!{){([A-Za-z_][A-Za-z0-9_]*)}(?!})")
_PROMPT_PLACEHOLDER_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PromptTemplateError(ValueError):
    """Prompt placeholders are outside MindRoom's deliberately small syntax."""


def _validate_prompt_template_field(
    field_name: str,
    *,
    format_spec: str,
    conversion: str | None,
) -> None:
    if not field_name:
        msg = "Empty prompt placeholders are not supported"
        raise PromptTemplateError(msg)
    if format_spec:
        msg = f"Prompt placeholder format specs are not supported: {field_name}"
        raise PromptTemplateError(msg)
    if conversion is not None:
        msg = f"Prompt placeholder conversions are not supported: {field_name}"
        raise PromptTemplateError(msg)
    if "." in field_name or "[" in field_name or "]" in field_name:
        msg = f"Compound prompt placeholders are not supported: {field_name}"
        raise PromptTemplateError(msg)
    if "{" in field_name or "}" in field_name or _PROMPT_PLACEHOLDER_FIELD_RE.fullmatch(field_name) is None:
        msg = f"Only bare prompt placeholder names are supported: {field_name}"
        raise PromptTemplateError(msg)


def _parse_prompt_template(template: str) -> tuple[tuple[str, str | None, str | None, str | None], ...]:
    try:
        parsed_parts = tuple(Formatter().parse(template))
    except ValueError as exc:
        raise PromptTemplateError(str(exc)) from exc

    parsed_field_count = 0
    for _, field_name, format_spec, conversion in parsed_parts:
        if field_name is None:
            continue
        parsed_field_count += 1
        _validate_prompt_template_field(
            field_name,
            format_spec=format_spec or "",
            conversion=conversion,
        )

    # Formatter exposes `{name}` and `{name:}` with the same empty format spec, so keep an exact-token check too.
    exact_placeholder_count = sum(1 for _ in _PROMPT_PLACEHOLDER_RE.finditer(template))
    if exact_placeholder_count != parsed_field_count:
        msg = "Only exact {field_name} prompt placeholders are supported"
        raise PromptTemplateError(msg)

    return parsed_parts


def _prompt_template_field_names(template: str) -> frozenset[str]:
    """Return exact placeholder names used by one MindRoom prompt template."""
    return frozenset(field_name for _, field_name, _, _ in _parse_prompt_template(template) if field_name is not None)


def render_prompt_template(template: str, **kwargs: object) -> str:
    """Render a MindRoom prompt template with exact placeholder replacement only."""
    rendered_parts: list[str] = []
    for literal_text, field_name, _, _ in _parse_prompt_template(template):
        rendered_parts.append(literal_text)
        if field_name is None:
            continue
        if field_name not in kwargs:
            msg = f"Missing prompt placeholder value: {field_name}"
            raise PromptTemplateError(msg)
        rendered_parts.append(str(kwargs[field_name]))
    return "".join(rendered_parts)


def build_agent_identity_context(
    *,
    display_name: str,
    matrix_id: str,
    model_provider: str,
    model_id: str,
    identity_context_template: str,
    openai_compat_history_guidance: str,
    include_openai_compat_guidance: bool = False,
) -> str:
    """Render the shared identity prompt with optional OpenAI-compatible guidance."""
    return render_prompt_template(
        identity_context_template,
        display_name=display_name,
        matrix_id=matrix_id,
        model_provider=model_provider,
        model_id=model_id,
        openai_compat_history_guidance=(openai_compat_history_guidance if include_openai_compat_guidance else ""),
    )


def validate_prompt_template_fields(prompt_name: str, prompt_text: str) -> None:
    """Validate one configured prompt override against its runtime field contract."""
    allowed_fields = PROMPT_TEMPLATE_FIELDS.get(prompt_name)
    if allowed_fields is None:
        return

    try:
        field_names = _prompt_template_field_names(prompt_text)
    except PromptTemplateError as exc:
        msg = f"Invalid prompt placeholder syntax for prompt override {prompt_name}: {exc}"
        raise ValueError(msg) from exc

    unsupported_fields = sorted(field_names - allowed_fields)
    if unsupported_fields:
        unsupported = ", ".join(unsupported_fields)
        allowed = ", ".join(sorted(allowed_fields))
        msg = (
            f"Unsupported prompt placeholder(s) for prompt override {prompt_name}: {unsupported}. "
            f"Allowed placeholders: {allowed}"
        )
        raise ValueError(msg)
