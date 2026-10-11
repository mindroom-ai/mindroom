"""Per-user monthly caps, kept as runtime state so they can change without editing config."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 - pydantic resolves UserCap's fields at runtime
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from mindroom import constants

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@dataclass(frozen=True, slots=True)
class UserCap:
    """One canonical user's monthly cap and who last set it."""

    limit_usd: float
    updated_at: datetime
    updated_by: str


class _CapsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    users: dict[str, UserCap]


def budget_caps_path(runtime_paths: RuntimePaths) -> Path:
    """Return where one runtime keeps its per-user caps."""
    return runtime_paths.storage_root / "budgets" / "user_caps.json"


@dataclass
class BudgetCaps:
    """Caps by canonical Matrix user ID, read lock-free and written atomically to one JSON file."""

    path: Path
    _entries: Mapping[str, UserCap] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def load(cls, path: Path) -> BudgetCaps:
        """Load the caps at ``path``; a missing file holds no caps."""
        caps = cls(path)
        if not path.exists():
            return caps
        try:
            users = _CapsFile.model_validate_json(path.read_bytes()).users
        except ValidationError as exc:
            msg = f"Invalid budget caps file {path}: {exc}"
            raise ValueError(msg) from exc
        caps._entries = MappingProxyType(users)
        return caps

    def get(self, user_id: str) -> UserCap | None:
        """Return the cap stored for exactly this user ID."""
        return self._entries.get(user_id)

    def entries(self) -> Mapping[str, UserCap]:
        """Return every stored cap."""
        return self._entries

    def set(self, user_id: str, limit_usd: float, *, updated_by: str, now: datetime) -> UserCap:
        """Create or replace one user's cap."""
        cap = UserCap(limit_usd=float(limit_usd), updated_at=now, updated_by=updated_by)
        with self._lock:
            self._write({**self._entries, user_id: cap})
        return cap

    def delete(self, user_id: str) -> None:
        """Remove one user's cap, if any."""
        with self._lock:
            if user_id in self._entries:
                self._write({key: cap for key, cap in self._entries.items() if key != user_id})

    def _write(self, users: dict[str, UserCap]) -> None:
        """Persist ``users`` first, so memory never holds caps a restart would lose."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(_CapsFile(users=users).model_dump_json(indent=2), encoding="utf-8")
        constants.safe_replace(tmp_path, self.path)
        self._entries = MappingProxyType(users)
