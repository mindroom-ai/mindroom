"""Rendered runtime chart checks for the approved-egress proxy PodDisruptionBudget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.test_helm_instance_worker_isolation import _resource
from tests.test_runtime_chart_approved_egress_rollout import PROXY_NAME, _render

if TYPE_CHECKING:
    from pathlib import Path


def _budgets(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == "PodDisruptionBudget"]


def test_proxy_budget_is_opt_in_and_needs_approved_egress(tmp_path: Path) -> None:
    """No budget renders by default, and enabling it without the proxy renders none either."""
    assert _budgets(_render(tmp_path, {})) == []
    assert _budgets(_render(tmp_path, {"enabled": False, "podDisruptionBudget": {"create": True}})) == []


def test_proxy_budget_selects_exactly_the_proxy_pod(tmp_path: Path) -> None:
    """The budget blocks eviction of the single ready proxy pod, lets an unready one go, and spares the primary."""
    docs = _render(tmp_path, {"podDisruptionBudget": {"create": True}, "podLabels": {"example.com/team": "infra"}})

    budget = _resource(docs, "PodDisruptionBudget", PROXY_NAME)
    proxy = _resource(docs, "Deployment", PROXY_NAME)
    selector = budget["spec"]["selector"]["matchLabels"]
    assert budget["spec"] == {
        "selector": {"matchLabels": proxy["spec"]["selector"]["matchLabels"]},
        "maxUnavailable": 0,
        "unhealthyPodEvictionPolicy": "AlwaysAllow",
    }
    assert selector.items() <= proxy["spec"]["template"]["metadata"]["labels"].items()
    primary_labels = _resource(docs, "Deployment", "mindroom-runtime")["spec"]["template"]["metadata"]["labels"]
    assert not selector.items() <= primary_labels.items()
    assert [doc["metadata"]["name"] for doc in _budgets(docs)] == [PROXY_NAME]
