"""Behavioral tests for the shared libyaml-preferring YAML helpers."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
import yaml

from mindroom import yaml_io

if TYPE_CHECKING:
    from pathlib import Path

_SAMPLE_DOCUMENT = """\
thread:
  id: "$abc:example.org"
  message_count: 2
messages:
  - sender: "@user:example.org"
    body: "hëllo — unicode, quotes ' and \\" included"
    timestamp: 1752605000000
  - sender: "@agent_code:example.org"
    body: |
      multi
      line
"""


def test_safe_load_matches_pyyaml_safe_load() -> None:
    """The helper should parse exactly like yaml.safe_load."""
    assert yaml_io.safe_load(_SAMPLE_DOCUMENT) == yaml.safe_load(_SAMPLE_DOCUMENT)


def test_safe_load_accepts_streams() -> None:
    """File-like streams should work, matching yaml.safe_load's interface."""
    assert yaml_io.safe_load(io.StringIO("a: 1")) == {"a": 1}


def test_safe_load_accepts_bytes_and_binary_streams() -> None:
    """Byte inputs should work, matching yaml.safe_load's interface."""
    document_bytes = _SAMPLE_DOCUMENT.encode()
    assert yaml_io.safe_load(document_bytes) == yaml.safe_load(document_bytes)
    assert yaml_io.safe_load(io.BytesIO(b"a: 1")) == {"a": 1}


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("x: 1" + ":1" * 32, id="base-60-int"),
        pytest.param("x: !!int '1" + ":1" * 32 + "'", id="tagged-base-60-int"),
        pytest.param("x: !!int {=: '1" + ":1" * 32 + "'}", id="mapping-base-60-int"),
        pytest.param("{" + "<<: {}, " * 65 + "a: 1}", id="merge-keys"),
        pytest.param("{" + "".join(f"{k}: 0, " for k in range(1025)) + "}", id="int-keys"),
        pytest.param("{" + "".join(f"{k}.5: 0, " for k in range(1025)) + "}", id="float-keys"),
        pytest.param("{<<: {0: 0}, " + "".join(f"{k}: 0, " for k in range(1, 1025)) + "}", id="merged-int-keys"),
        pytest.param("!!set {" + "".join(f"{k}, " for k in range(1025)) + "}", id="int-set"),
        pytest.param("x: 0000-01-01", id="year-0-date"),
        pytest.param("x: !!bool maybe", id="mistagged-bool"),
        pytest.param("".join(f"%TAG !t{k}! x\n" for k in range(17)) + "--- a\n", id="tag-directives"),
        pytest.param(
            "".join(f"%TAG !t{k}! x" + "\r\x85\u2028\u2029"[k % 4] for k in range(17)) + "--- a\n",
            id="tag-directives-after-other-line-breaks",
        ),
    ],
)
def test_safe_load_without_aliases_refuses_costly_or_unbuildable_values(document: str) -> None:
    """Worker-written YAML must not take superlinear time to build or escape callers that catch only YAML errors."""
    with pytest.raises(yaml.YAMLError):
        yaml_io.safe_load_without_aliases(document)


def test_safe_load_without_aliases_refuses_tag_directives_before_composing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``%TAG`` prefix is copied into every node naming its handle, so a short document is refused before it is composed."""
    composed: list[object] = []
    monkeypatch.setattr(yaml, "load", lambda stream, **_kwargs: composed.append(stream))
    document = "%TAG !t! " + "A" * 4096 + "\r---\n" + "- !t!a\n" * 100
    with pytest.raises(yaml.YAMLError, match="%TAG directives are not allowed"):
        yaml_io.safe_load_without_aliases(document)
    assert composed == []


def test_safe_load_without_aliases_builds_values_at_the_limits() -> None:
    """A directive, short base-60 integers, a few merge keys, and many numeric keys still load exactly like ``safe_load``."""
    numeric_keys = "".join(f"{k}: 0, " for k in range(1024))
    document = "%YAML 1.1\n---\nx: 12" + ":1" * 31 + "\ny: {" + "<<: {a: 1}, " * 64 + "b: 2}\nz: {" + numeric_keys + "}\n"
    assert yaml_io.safe_load_without_aliases(document) == yaml_io.safe_load(document)


def test_safe_dump_roundtrips() -> None:
    """Dumped documents should parse back to the original data."""
    data = yaml.safe_load(_SAMPLE_DOCUMENT)
    text = yaml_io.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    assert yaml_io.safe_load(text) == data


def test_safe_dump_to_stream_returns_none() -> None:
    """Dumping into a stream should write there and return None."""
    stream = io.StringIO()
    assert yaml_io.safe_dump({"a": 1}, stream) is None
    assert yaml_io.safe_load(stream.getvalue()) == {"a": 1}


def test_safe_dump_forwards_pyyaml_options() -> None:
    """Standard dump options should pass through with PyYAML semantics."""
    data = {"outer": {"message": "a deliberately long value"}}
    expected = yaml.dump(
        data,
        Dumper=yaml_io._SAFE_DUMPER,
        explicit_start=True,
        indent=4,
        width=20,
    )
    assert yaml_io.safe_dump(data, explicit_start=True, indent=4, width=20) == expected


def test_safe_dump_accepts_encoded_binary_streams() -> None:
    """Encoded binary streams should work exactly like yaml.safe_dump."""
    actual = io.BytesIO()
    expected = io.BytesIO()
    assert yaml_io.safe_dump({"a": "é"}, actual, encoding="utf-8", allow_unicode=True) is None
    assert yaml.safe_dump({"a": "é"}, expected, encoding="utf-8", allow_unicode=True) is None
    assert actual.getvalue() == expected.getvalue()


def test_safe_dump_rejects_unsafe_types_like_safe_dump() -> None:
    """Arbitrary Python objects should be rejected, matching yaml.safe_dump."""

    class Unrepresentable:
        pass

    with pytest.raises(yaml.YAMLError):
        yaml_io.safe_dump({"bad": Unrepresentable()})


def test_write_text_atomic_replaces_content_and_preserves_mode(tmp_path: Path) -> None:
    """Atomic text replacement must retain file permissions and leave no temp file."""
    path = tmp_path / "config.yaml"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o640)

    yaml_io.write_text_atomic(path, "new\n")

    assert path.read_text(encoding="utf-8") == "new\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [path]


def test_prefers_libyaml_classes_when_available() -> None:
    """When PyYAML was built with libyaml, the C classes must be selected."""
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML built without libyaml")
    assert yaml_io.SafeLoader is yaml.CSafeLoader
    assert yaml_io._SAFE_DUMPER is yaml.CSafeDumper
