"""Safe YAML load/dump helpers that prefer the fast libyaml-backed classes.

``yaml.safe_load``/``yaml.safe_dump`` always use the pure-Python loader and
dumper, even when PyYAML was built with libyaml. The C classes parse and
serialize 10-20x faster with identical semantics for the safe tag set, so
every safe load/dump in this codebase should go through this module.

The config ``!include`` loader (``mindroom.config.yaml_includes``) deliberately
stays on the pure-Python ``SafeLoader``: it renames the stream so error marks
point at the offending config file, which the C parser does not support, and
config parsing is cold.
"""

from __future__ import annotations

from typing import IO, Any, TypedDict, Unpack, overload

import yaml

try:
    from yaml import CSafeDumper, CSafeLoader
except ImportError:
    from yaml import SafeDumper, SafeLoader

    _SAFE_DUMPER = SafeDumper
    _SAFE_LOADER = SafeLoader
else:
    _SAFE_DUMPER = CSafeDumper
    _SAFE_LOADER = CSafeLoader


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
    return yaml.load(stream, Loader=_SAFE_LOADER)  # noqa: S506 - safe loader variant


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
