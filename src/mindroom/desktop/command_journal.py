"""Durable replay state for locally executed Matrix desktop commands."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn, cast

from mindroom.desktop.protocol import DesktopProtocolError, DesktopResponse
from mindroom.durable_write import write_json_file_durable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.desktop.protocol import DesktopCommand

_JOURNAL_VERSION = 1
_MAX_REPLAY_RESPONSES = 1024
_MAX_TRACKED_SESSIONS = 128


class DesktopCommandJournalError(RuntimeError):
    """The durable desktop command journal is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class DesktopCommandJournalEntry:
    """One started or completed desktop command."""

    command_fingerprint: str
    response: DesktopResponse | None


@dataclass
class DesktopCommandJournal:
    """Bounded durable record preventing control replay after process restarts."""

    path: Path | None
    entries: OrderedDict[str, DesktopCommandJournalEntry] = field(default_factory=OrderedDict)
    sequence_high_watermarks: OrderedDict[str, int] = field(default_factory=OrderedDict)

    @classmethod
    def load(cls, path: Path | None) -> DesktopCommandJournal:
        """Load a journal, or create an in-memory journal when no path is supplied."""
        journal = cls(path=path)
        if path is None:
            return journal
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return journal
        except (UnicodeError, json.JSONDecodeError) as exc:
            msg = f"Desktop command journal {path} is unreadable or malformed."
            raise DesktopCommandJournalError(msg) from exc
        journal._load_payload(raw)
        return journal

    def get(self, request_id: str) -> DesktopCommandJournalEntry | None:
        """Return one previously started command without mutating replay order."""
        return self.entries.get(request_id)

    def sequence_error(self, command: DesktopCommand) -> str | None:
        """Return the replay error for a non-increasing command sequence."""
        previous = self.sequence_high_watermarks.get(command.session_id)
        if previous is not None and command.sequence <= previous:
            return "Desktop command sequence was already used or arrived out of order."
        return None

    def remember_started(self, command: DesktopCommand, command_fingerprint: str) -> None:
        """Durably record accepted work before any local side effect can begin."""
        self.entries[command.request_id] = DesktopCommandJournalEntry(
            command_fingerprint=command_fingerprint,
            response=None,
        )
        self.entries.move_to_end(command.request_id)
        self.sequence_high_watermarks[command.session_id] = command.sequence
        self.sequence_high_watermarks.move_to_end(command.session_id)
        self._prune()
        self._persist()

    def remember_response(
        self,
        command: DesktopCommand,
        command_fingerprint: str,
        response: DesktopResponse,
    ) -> None:
        """Durably record the exact response returned for a command."""
        existing = self.entries.get(command.request_id)
        if existing is not None and existing.command_fingerprint != command_fingerprint:
            msg = f"Desktop request ID {command.request_id} was journaled with different command content."
            raise DesktopCommandJournalError(msg)
        self.entries[command.request_id] = DesktopCommandJournalEntry(
            command_fingerprint=command_fingerprint,
            response=response,
        )
        self.entries.move_to_end(command.request_id)
        self._prune()
        self._persist()

    def _prune(self) -> None:
        while len(self.entries) > _MAX_REPLAY_RESPONSES:
            self.entries.popitem(last=False)
        while len(self.sequence_high_watermarks) > _MAX_TRACKED_SESSIONS:
            self.sequence_high_watermarks.popitem(last=False)

    def _persist(self) -> None:
        if self.path is None:
            return
        payload = {
            "v": _JOURNAL_VERSION,
            "entries": [
                {
                    "request_id": request_id,
                    "command_fingerprint": entry.command_fingerprint,
                    "response": entry.response.to_content() if entry.response is not None else None,
                }
                for request_id, entry in self.entries.items()
            ],
            "sequence_high_watermarks": [
                {"session_id": session_id, "sequence": sequence}
                for session_id, sequence in self.sequence_high_watermarks.items()
            ],
        }
        write_json_file_durable(self.path, payload, indent=2, sort_keys=True, trailing_newline=True)
        self.path.chmod(0o600)

    def _load_payload(self, raw: object) -> None:
        payload = self._string_keyed_object(raw)
        if payload.get("v") != _JOURNAL_VERSION:
            self._raise_malformed()
        raw_entries = payload.get("entries")
        raw_sequences = payload.get("sequence_high_watermarks")
        if not isinstance(raw_entries, list) or not isinstance(raw_sequences, list):
            self._raise_malformed()
        if len(raw_entries) > _MAX_REPLAY_RESPONSES or len(raw_sequences) > _MAX_TRACKED_SESSIONS:
            self._raise_malformed()

        self._load_entries(raw_entries)
        self._load_sequences(raw_sequences)

    def _load_entries(self, raw_entries: Sequence[object]) -> None:
        for raw_entry in raw_entries:
            entry = self._string_keyed_object(raw_entry)
            request_id = self._bounded_identifier(entry.get("request_id"))
            fingerprint = entry.get("command_fingerprint")
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
                or request_id in self.entries
            ):
                self._raise_malformed()
            response_raw = entry.get("response")
            try:
                response = DesktopResponse.from_content(response_raw) if response_raw is not None else None
            except DesktopProtocolError as exc:
                self._raise_malformed(exc)
            if response is not None and response.request_id != request_id:
                self._raise_malformed()
            self.entries[request_id] = DesktopCommandJournalEntry(fingerprint, response)

    def _load_sequences(self, raw_sequences: Sequence[object]) -> None:
        for raw_sequence in raw_sequences:
            sequence_record = self._string_keyed_object(raw_sequence)
            session_id = self._bounded_identifier(sequence_record.get("session_id"))
            sequence = sequence_record.get("sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
                or session_id in self.sequence_high_watermarks
            ):
                self._raise_malformed()
            self.sequence_high_watermarks[session_id] = sequence

    @staticmethod
    def _string_keyed_object(value: object) -> dict[str, object]:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            DesktopCommandJournal._raise_malformed()
        return cast("dict[str, object]", value)

    @staticmethod
    def _bounded_identifier(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 128:
            DesktopCommandJournal._raise_malformed()
        return value

    @staticmethod
    def _raise_malformed(cause: Exception | None = None) -> NoReturn:
        msg = "Desktop command journal has an unsupported or malformed payload."
        if cause is None:
            raise DesktopCommandJournalError(msg)
        raise DesktopCommandJournalError(msg) from cause


__all__ = [
    "DesktopCommandJournal",
    "DesktopCommandJournalEntry",
    "DesktopCommandJournalError",
]
