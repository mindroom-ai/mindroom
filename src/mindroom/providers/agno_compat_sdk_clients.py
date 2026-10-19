"""Keep garbage-collected provider SDK clients from unregistering sockets that new connections reuse."""

from __future__ import annotations

import sys

# AGNO_COMPAT: Agno models leave closing their provider SDK clients to an unsafe SDK finalizer.
# Reason: Agno models create OpenAI, Anthropic, Groq, and Cerebras SDK clients without a close
# lifecycle, so MindRoom drops models whose clients still pool idle keep-alive connections, and the
# cyclic garbage collector reclaims them. In that collection asyncio's transport finalizers close the
# sockets and free their file descriptors, while each SDK's `AsyncHttpxClientWrapper.__del__` schedules
# `aclose()`. That close runs later and unregisters the freed descriptor numbers from the event loop,
# which by then can belong to new connections, so their connect or response never completes.
# Without the SDK finalizer the transport finalizers alone close the sockets, and nothing touches
# the event loop afterwards.
# Upstream issue: Tracking gap for this race (searched October 18, 2026); the related finalizer
# reports https://github.com/openai/openai-python/issues/2440 (closed as not planned) and
# https://github.com/anthropics/anthropic-sdk-python/issues/807 (closed by skipping already closed
# clients) do not cover it, and Agno has no model client close API.
# Upstream PR: None identified.
# Remove when: these SDKs no longer close clients from `AsyncHttpxClientWrapper.__del__`, or Agno
# exposes model client lifetimes and MindRoom closes every model's client.
# Coverage: tests/test_agno_compat_sdk_clients.py::test_collected_sdk_client_leaves_reused_descriptor_registered.
_SDK_CLIENT_MODULES = (
    "openai._base_client",
    "anthropic._base_client",
    "groq._base_client",
    "cerebras.cloud.sdk._base_client",
)


def remove_sdk_client_finalizers() -> None:
    """Drop the closing finalizer from each loaded provider SDK's default async HTTP client."""
    for module_name in _SDK_CLIENT_MODULES:
        module = sys.modules.get(module_name)
        wrapper = getattr(module, "AsyncHttpxClientWrapper", None) if module is not None else None
        if wrapper is not None and "__del__" in vars(wrapper):
            del wrapper.__del__
