"""Rendered runtime chart checks that approved-egress allowlist edits reach the proxy."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

RUNTIME_CHART_DIR = Path(__file__).resolve().parents[1] / "cluster" / "k8s" / "runtime"
PROXY_NAME = "mindroom-runtime-egress-proxy"


def _render_text(tmp_path: Path, approved_egress: dict[str, Any]) -> str:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    values = {
        "eventCache": {"postgres": {"auth": {"password": "test-password"}}},
        "workers": {"backend": "kubernetes", "sandbox": {"proxyToken": {"value": "test-token"}}},
        "approvedEgress": {"enabled": True, "image": {"tag": "v0.1.10"}, **approved_egress},
    }
    values_path = tmp_path / "values.yaml"
    values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
    completed = subprocess.run(
        [helm, "template", "mindroom-runtime", str(RUNTIME_CHART_DIR), "--values", str(values_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _render(tmp_path: Path, approved_egress: dict[str, Any]) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_render_text(tmp_path, approved_egress)) if isinstance(doc, dict)]


def _deployment(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"] == name)


def _proxy_annotations(docs: list[dict[str, Any]]) -> dict[str, str]:
    return _deployment(docs, PROXY_NAME)["spec"]["template"]["metadata"].get("annotations", {})


def test_inline_allowlist_changes_roll_only_the_proxy(tmp_path: Path) -> None:
    """The subPath-mounted allowlist is read at proxy startup, so its content hash belongs on the proxy pod only."""
    first = _render(tmp_path, {"allowlist": {"domains": ["example.com", ".docs.example.com"]}})
    second = _render(tmp_path, {"allowlist": {"domains": ["example.com"]}})

    assert (
        _proxy_annotations(first)["checksum/allowlist"]
        == hashlib.sha256(
            b"example.com\n.docs.example.com\n",
        ).hexdigest()
    )
    assert _proxy_annotations(second)["checksum/allowlist"] == hashlib.sha256(b"example.com\n").hexdigest()
    assert _deployment(first, "mindroom-runtime") == _deployment(second, "mindroom-runtime")


def test_inline_allowlist_checksum_keeps_pod_annotations_and_squid_checksum(tmp_path: Path) -> None:
    """The allowlist checksum sits beside user pod annotations and the parent-proxy Squid checksum."""
    docs = _render(
        tmp_path,
        {"podAnnotations": {"example.test/owner": "platform"}, "parentProxy": {"enabled": True}},
    )

    assert set(_proxy_annotations(docs)) == {"example.test/owner", "checksum/allowlist", "checksum/squid-config"}
    assert _proxy_annotations(docs)["checksum/allowlist"] == hashlib.sha256(b"").hexdigest()


def test_chart_allowlist_checksum_replaces_a_pod_annotation_with_the_same_key(tmp_path: Path) -> None:
    """A stale user annotation must not render a duplicate key or override the chart-computed checksum."""
    text = _render_text(
        tmp_path,
        {"allowlist": {"domains": ["example.com"]}, "podAnnotations": {"checksum/allowlist": "stale"}},
    )

    assert text.count("checksum/allowlist:") == 1
    assert _proxy_annotations(
        [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)],
    ) == {"checksum/allowlist": hashlib.sha256(b"example.com\n").hexdigest()}


def test_existing_allowlist_configmap_uses_operator_supplied_checksum(tmp_path: Path) -> None:
    """The chart cannot hash an external ConfigMap, so only an operator-supplied annotation rolls the proxy."""
    assert "checksum/allowlist" not in _proxy_annotations(
        _render(tmp_path, {"allowlist": {"existingConfigMap": "egress-allowlist"}}),
    )

    docs = _render(
        tmp_path,
        {"allowlist": {"existingConfigMap": "egress-allowlist"}, "podAnnotations": {"checksum/allowlist": "abc123"}},
    )

    assert _proxy_annotations(docs) == {"checksum/allowlist": "abc123"}
