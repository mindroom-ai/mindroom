"""Safe YAML load/dump helpers that prefer the fast libyaml-backed classes.

``yaml.safe_load``/``yaml.safe_dump`` always use the pure-Python loader and
dumper, even when PyYAML was built with libyaml. The C classes parse and
serialize 10-20x faster with identical semantics for the safe tag set, so
every safe load/dump in this codebase should go through this module.

Input that untrusted code can write, such as workspace files that worker code
shares, goes through ``safe_load_untrusted`` instead: libyaml's composer
recurses in C, so deeply nested input overflows the C stack and kills the
process, where the pure-Python loader raises ``RecursionError``. It also
refuses aliases, because a few hundred bytes of nested aliases expand to
gigabytes once anything serializes the result.

``SafeLoader`` is also the base for custom safe loaders, so they share the
same libyaml preference and pure-Python fallback.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import IO, Any, TypedDict, Unpack, overload

import yaml
from yaml.composer import ComposerError

try:
    from yaml import CSafeDumper
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeDumper, SafeLoader

    _SAFE_DUMPER = SafeDumper
else:
    _SAFE_DUMPER = CSafeDumper


class _DumpOptions(TypedDict, total=False):
    default_style: str | None
    default_flow_style: bool | None
    canonical: bool | None
    indent: int | None
    width: int | None
    allow_unicode: bool | None
    line_break: str | None
    explicit_start: bool | None
    explicit_end: bool | None
    version: tuple[int, int] | None
    tags: dict[str, str] | None
    sort_keys: bool


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:  # noqa: ANN401
    """Parse one YAML document like ``yaml.safe_load``, preferring libyaml."""
    return yaml.load(stream, Loader=SafeLoader)


class _UntrustedSafeLoader(yaml.SafeLoader):
    """Pure-Python safe loader that refuses aliases, which untrusted input can nest into an expansion bomb."""

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            message = "YAML aliases are not accepted in untrusted input"
            raise ComposerError(None, None, message, event.start_mark)
        return super().compose_node(parent, index)


def safe_load_untrusted(stream: str) -> Any:  # noqa: ANN401
    """Parse one YAML document that untrusted code can write, with the pure-Python loader and no aliases."""
    return yaml.load(stream, Loader=_UntrustedSafeLoader)  # noqa: S506 - a SafeLoader subclass


@overload
def safe_dump(
    data: object,
    stream: None = None,
    *,
    encoding: None = None,
    **kwargs: Unpack[_DumpOptions],
) -> str: ...


@overload
def safe_dump(
    data: object,
    stream: None = None,
    *,
    encoding: str,
    **kwargs: Unpack[_DumpOptions],
) -> bytes: ...


@overload
def safe_dump(
    data: object,
    stream: IO[str],
    *,
    encoding: str | None = None,
    **kwargs: Unpack[_DumpOptions],
) -> None: ...


@overload
def safe_dump(
    data: object,
    stream: IO[bytes],
    *,
    encoding: str,
    **kwargs: Unpack[_DumpOptions],
) -> None: ...


def safe_dump(
    data: object,
    stream: IO[str] | IO[bytes] | None = None,
    *,
    encoding: str | None = None,
    **kwargs: Unpack[_DumpOptions],
) -> str | bytes | None:
    """Serialize like ``yaml.safe_dump``, preferring libyaml."""
    return yaml.dump(
        data,
        stream,
        Dumper=_SAFE_DUMPER,
        encoding=encoding,
        **kwargs,
    )


def write_text_atomic(path: Path, content: str) -> None:
    """Replace an existing text file after fully writing a sibling temp file."""
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(content.encode("utf-8"))
        temp_file.flush()
        os.fsync(temp_file.fileno())
    temp_path.chmod(path.stat().st_mode & 0o777)
    try:
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
