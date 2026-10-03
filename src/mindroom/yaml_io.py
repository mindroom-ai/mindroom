"""Safe YAML load/dump helpers that prefer the fast libyaml-backed classes.

``yaml.safe_load``/``yaml.safe_dump`` always use the pure-Python loader and
dumper, even when PyYAML was built with libyaml. The C classes parse and
serialize 10-20x faster with identical semantics for the safe tag set, so
every safe load/dump in this codebase should go through this module.

``SafeLoader`` is also the base for custom safe loaders, so they share the
same libyaml preference and pure-Python fallback.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import IO, Any, TypedDict, Unpack, overload

import yaml
from yaml.constructor import ConstructorError

try:
    from yaml import CSafeDumper
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeDumper, SafeLoader

    _SAFE_DUMPER = SafeDumper
else:
    _SAFE_DUMPER = CSafeDumper

_MAX_UNTRUSTED_DEPTH = 64
_MAX_UNTRUSTED_DIRECTIVES = 16
_MAX_UNTRUSTED_NODES = 250_000
_MAX_UNTRUSTED_MERGE_KEYS = 64
_MAX_UNTRUSTED_NUMERIC_KEYS = 1024
_MAX_UNTRUSTED_BASE_60_LENGTH = 64
_NUMERIC_TAGS = frozenset({"tag:yaml.org,2002:int", "tag:yaml.org,2002:float"})
# Every line break libyaml recognizes; only a ``%`` at the start of a line can begin a directive.
_YAML_LINE_BREAKS = "\n\r\x85\u2028\u2029"


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


class _UntrustedLoader(SafeLoader):  # ty: ignore[unsupported-base] - both safe loader variants have the same interface
    """``SafeLoader`` that refuses values whose construction time grows faster than their length."""

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        """Refuse mappings with many merge keys or many integer and float keys, which build in quadratic time.

        Each merge key is deleted from the middle of the mapping's entries, and numeric keys can share one hash.
        Sets are mappings too, so this also bounds ``!!set`` members.
        """
        if sum(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value) > _MAX_UNTRUSTED_MERGE_KEYS:
            msg = f"YAML mappings may hold {_MAX_UNTRUSTED_MERGE_KEYS} merge keys at most"
            raise ConstructorError(None, None, msg, node.start_mark)
        super().flatten_mapping(node)
        if sum(key.tag in _NUMERIC_TAGS for key, _ in node.value) > _MAX_UNTRUSTED_NUMERIC_KEYS:
            msg = f"YAML mappings may hold {_MAX_UNTRUSTED_NUMERIC_KEYS} integer or float keys at most"
            raise ConstructorError(None, None, msg, node.start_mark)

    def construct_yaml_int(self, node: yaml.Node) -> int:
        """Refuse long base-60 integers, which convert in time quadratic in their length; base-60 floats overflow first."""
        # SafeConstructor also builds an int from the ``=`` entry of a mapping node, so check the scalar it reads.
        value = self.construct_scalar(node)
        if ":" in value and len(value) > _MAX_UNTRUSTED_BASE_60_LENGTH:
            msg = f"YAML base-60 integers may hold {_MAX_UNTRUSTED_BASE_60_LENGTH} characters at most"
            raise ConstructorError(None, None, msg, node.start_mark)
        return super().construct_yaml_int(node)


_UntrustedLoader.add_constructor("tag:yaml.org,2002:int", _UntrustedLoader.construct_yaml_int)


def safe_load_without_aliases(stream: str) -> Any:  # noqa: ANN401
    """Parse like ``safe_load`` but refuse documents whose composed tree could exhaust memory or the C stack.

    Directives are counted before parsing, because libyaml compares each ``%TAG`` directive with every earlier one.
    Events are checked before any node is composed: aliases let a short document describe a larger tree,
    the libyaml composer recurses in C once per nesting level, and each composed node costs a few hundred bytes.
    Construction refuses values whose cost grows faster than their length and reports every failure as ``yaml.YAMLError``.
    """
    directives = stream.startswith("%") + sum(stream.count(f"{line_break}%") for line_break in _YAML_LINE_BREAKS)
    if directives > _MAX_UNTRUSTED_DIRECTIVES:
        msg = f"YAML may hold {_MAX_UNTRUSTED_DIRECTIVES} directives at most"
        raise yaml.YAMLError(msg)
    depth = 0
    nodes = 0
    for event in yaml.parse(stream, Loader=SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            msg = "YAML aliases are not allowed"
            raise yaml.YAMLError(msg)
        if isinstance(event, yaml.CollectionEndEvent):
            depth -= 1
        elif isinstance(event, yaml.NodeEvent):
            nodes += 1
            depth += isinstance(event, yaml.CollectionStartEvent)
            if depth > _MAX_UNTRUSTED_DEPTH or nodes > _MAX_UNTRUSTED_NODES:
                msg = f"YAML may nest {_MAX_UNTRUSTED_DEPTH} levels and hold {_MAX_UNTRUSTED_NODES} nodes at most"
                raise yaml.YAMLError(msg)
    try:
        return yaml.load(stream, Loader=_UntrustedLoader)  # noqa: S506
    except yaml.YAMLError:
        raise
    except Exception as exc:
        # SafeConstructor lets builtin errors escape for values it cannot build, such as ValueError for a year-0 date.
        msg = f"YAML value cannot be constructed: {exc}"
        raise yaml.YAMLError(msg) from exc


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
