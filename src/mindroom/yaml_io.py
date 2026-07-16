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

from typing import IO, Any, overload

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


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:  # noqa: ANN401
    """Parse one YAML document like ``yaml.safe_load``, preferring libyaml."""
    return yaml.load(stream, Loader=_SAFE_LOADER)  # noqa: S506 - safe loader variant


@overload
def safe_dump(
    data: object,
    stream: None = None,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = False,
) -> str: ...


@overload
def safe_dump(
    data: object,
    stream: IO[str],
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = False,
) -> None: ...


def safe_dump(
    data: object,
    stream: IO[str] | None = None,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = False,
) -> str | None:
    """Serialize like ``yaml.safe_dump``, preferring libyaml."""
    return yaml.dump(
        data,
        stream,
        Dumper=_SAFE_DUMPER,
        default_flow_style=default_flow_style,
        sort_keys=sort_keys,
        allow_unicode=allow_unicode,
    )
