"""The Jira and Confluence spaces one agent's Atlassian toolkit may use.

Jira calls its projects spaces, and JQL still names them ``project``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.oauth.atlassian import AtlassianProduct

JIRA_PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,254}")
CONFLUENCE_SPACE_KEY_PATTERN = re.compile(r"~?[A-Za-z0-9_-]{1,255}")
_KEY_PATTERNS = {"jira": JIRA_PROJECT_KEY_PATTERN, "confluence": CONFLUENCE_SPACE_KEY_PATTERN}
_QUERY_FIELDS = {"jira": "project", "confluence": "space"}
_ORDER_BY_PATTERN = re.compile(r"order\s+by\b", re.IGNORECASE)


def _space_keys(product: AtlassianProduct, value: object) -> tuple[str, ...] | None:
    """Return configured space keys without duplicates, rejecting anything that is not a list of space keys."""
    if value is None:
        return None
    field_name = f"{product}_spaces"
    if not isinstance(value, list):
        msg = f"{field_name} must be a list of space keys"
        raise TypeError(msg)
    keys: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str) or not _KEY_PATTERNS[product].fullmatch(item.strip()):
            msg = f"{field_name} must contain {product.capitalize()} space keys such as ENG, not {item!r}"
            raise ValueError(msg)
        keys.setdefault(item.strip().casefold(), item.strip())
    return tuple(keys.values())


def _order_by_start(query: str) -> int | None:
    """Return where a JQL or CQL ORDER BY clause starts outside quoted text, or None without one."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(query):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif (index == 0 or not (query[index - 1].isalnum() or query[index - 1] == "_")) and _ORDER_BY_PATTERN.match(
            query,
            index,
        ):
            return index
    return None


def space_limit_sentence(product: AtlassianProduct, keys: Sequence[str]) -> str:
    """Say which spaces of one product this agent may use, in its instructions and refusals alike."""
    return f"This agent may use only these {product.capitalize()} spaces: {', '.join(keys)}."


@dataclass(frozen=True, slots=True)
class AtlassianSpaces:
    """Space keys each product may use; None leaves that product unrestricted, and no keys disables it."""

    jira: tuple[str, ...] | None = None
    confluence: tuple[str, ...] | None = None

    @classmethod
    def from_config(cls, *, jira_spaces: object, confluence_spaces: object) -> AtlassianSpaces:
        """Validate the agent's configured keys before the toolkit offers any function."""
        return cls(jira=_space_keys("jira", jira_spaces), confluence=_space_keys("confluence", confluence_spaces))

    def keys(self, product: AtlassianProduct) -> tuple[str, ...] | None:
        """Return the keys one product may use, or None when it is unrestricted."""
        return self.jira if product == "jira" else self.confluence

    def instructions(self, products: Sequence[AtlassianProduct]) -> str | None:
        """Name the allowed spaces of each limited product offered, so the model does not reach for others."""
        limits = [space_limit_sentence(product, keys) for product in products if (keys := self.keys(product))]
        return " ".join(limits) or None

    def allows(self, product: AtlassianProduct, key: object) -> bool:
        """Return whether a space key reported by Atlassian or given by the model is one this agent may use."""
        keys = self.keys(product)
        return keys is None or (isinstance(key, str) and key.casefold() in {allowed.casefold() for allowed in keys})

    def restrict_query(self, product: AtlassianProduct, query: str) -> str:
        """Limit a JQL or CQL query to the allowed spaces, keeping its ORDER BY clause last.

        This only narrows what Atlassian returns: a query can still close the parentheses, so callers also drop
        every result outside the allowed spaces.
        """
        keys = self.keys(product)
        if keys is None:
            return query
        start = _order_by_start(query)
        condition, order_by = (query, "") if start is None else (query[:start], query[start:])
        quoted_keys = ", ".join(f'"{key}"' for key in keys)
        clause = f"{_QUERY_FIELDS[product]} in ({quoted_keys})"
        restricted = f"{clause} AND ({condition.strip()})" if condition.strip() else clause
        return f"{restricted} {order_by.strip()}" if order_by else restricted
