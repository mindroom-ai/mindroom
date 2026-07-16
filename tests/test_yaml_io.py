"""Behavioral tests for the shared libyaml-preferring YAML helpers."""

from __future__ import annotations

import io

import pytest
import yaml

from mindroom import yaml_io

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
    assert yaml_io.safe_dump(data, explicit_start=True, indent=4, width=20) == yaml.safe_dump(
        data,
        explicit_start=True,
        indent=4,
        width=20,
    )


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


def test_prefers_libyaml_classes_when_available() -> None:
    """When PyYAML was built with libyaml, the C classes must be selected."""
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML built without libyaml")
    assert yaml_io._SAFE_LOADER is yaml.CSafeLoader
    assert yaml_io._SAFE_DUMPER is yaml.CSafeDumper
