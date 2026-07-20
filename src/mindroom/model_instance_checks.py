"""Leaf isinstance checks against provider model classes without importing them.

An instance of a provider model class can only exist once the class's defining
module is imported, so probing ``sys.modules`` is semantically identical to
importing the class for an ``isinstance`` check — subclasses included — while
keeping provider SDKs out of import graphs that merely dispatch on model type
(#1436). Callers pass the class's concrete defining module (for example
``agno.models.azure.openai_chat``), not a package init that may re-export a
try/except stub.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agno.models.openai.chat import OpenAIChat
    from agno.models.openai.responses import OpenAIResponses

# Exact-type allowlist: the two agno base classes plus MindRoom's own
# wrappers, which production `provider: openai` loads and which add replay
# compatibility only, never client routing. Any OTHER subclass — including
# agno's OpenAI-compatible family (OpenAILike/Azure/OpenRouter/DeepSeek/
# llama.cpp, OpenResponses) and future plugins that could redirect the
# endpoint inside an overridden client builder — is untrusted by construction.
_TRUSTED_OPENAI_ENDPOINT_CLASSES = (
    ("agno.models.openai.chat", "OpenAIChat"),
    ("agno.models.openai.responses", "OpenAIResponses"),
    ("mindroom.openai_models", "MindRoomOpenAIChat"),
    ("mindroom.openai_models", "MindRoomOpenAIResponses"),
)


def isinstance_of_loaded(instance: object, *class_paths: tuple[str, str]) -> bool:
    """Return isinstance against the named classes, treating unloaded modules as no-match."""
    for module_name, class_name in class_paths:
        module = sys.modules.get(module_name)
        loaded_class = getattr(module, class_name, None) if module is not None else None
        if loaded_class is not None and isinstance(instance, loaded_class):
            return True
    return False


def _is_exact_type_of_loaded(instance: object, *class_paths: tuple[str, str]) -> bool:
    """Return whether the instance's exact type is one of the named classes.

    Subclasses deliberately do not match, and unloaded modules are no-match.
    """
    for module_name, class_name in class_paths:
        module = sys.modules.get(module_name)
        loaded_class = getattr(module, class_name, None) if module is not None else None
        if loaded_class is not None and type(instance) is loaded_class:
            return True
    return False


def is_genuine_openai_endpoint(model: object) -> bool:
    """Return whether requests for this model provably reach the real OpenAI API.

    Fail-closed: only an exact-type allowlist is trusted (``OpenAIChat``,
    ``OpenAIResponses``, and MindRoom's routing-neutral wrappers of the two),
    so any subclass — agno's OpenAI-compatible family covering Azure,
    OpenRouter, DeepSeek, llama.cpp, and friends, or a future plugin that
    redirects the endpoint inside an overridden client builder — is
    non-genuine by construction. A trusted instance must additionally have no
    instance ``base_url``, no ``client_params`` (agno merges those over the
    base client kwargs, so they can redirect the endpoint), no prebuilt or
    custom HTTP client, and no ``OPENAI_BASE_URL`` environment override
    (which the OpenAI SDK applies when no instance URL is set). Custom
    endpoints can serve arbitrary models under tiktoken-recognized ids, so
    only a provably genuine endpoint justifies trusting the model id to
    identify the serving tokenizer; genuine-OpenAI users who set
    ``client_params`` for timeouts and the like merely get the conservative
    byte-bound estimator.
    """
    if not _is_exact_type_of_loaded(model, *_TRUSTED_OPENAI_ENDPOINT_CLASSES):
        return False
    openai_model = cast("OpenAIChat | OpenAIResponses", model)
    if openai_model.base_url is not None or openai_model.client_params:
        return False
    if openai_model.client is not None or openai_model.async_client is not None:
        return False
    if openai_model.http_client is not None:
        return False
    # Presence beats truthiness: an empty-string override still means someone
    # tried to redirect the endpoint, so fail closed on it.
    return "OPENAI_BASE_URL" not in os.environ
