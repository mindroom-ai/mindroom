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

_OPENAI_CHAT_CLASS = ("agno.models.openai.chat", "OpenAIChat")
_OPENAI_RESPONSES_CLASS = ("agno.models.openai.responses", "OpenAIResponses")
_OPENAI_LIKE_CLASS = ("agno.models.openai.like", "OpenAILike")
_OPEN_RESPONSES_CLASS = ("agno.models.openai.open_responses", "OpenResponses")


def isinstance_of_loaded(instance: object, *class_paths: tuple[str, str]) -> bool:
    """Return isinstance against the named classes, treating unloaded modules as no-match."""
    for module_name, class_name in class_paths:
        module = sys.modules.get(module_name)
        loaded_class = getattr(module, class_name, None) if module is not None else None
        if loaded_class is not None and isinstance(instance, loaded_class):
            return True
    return False


def is_genuine_openai_endpoint(model: object) -> bool:
    """Return whether requests for this model provably reach the real OpenAI API.

    Fail-closed: this enumerates the exact conditions under which the endpoint
    is trusted, and any client customization it does not recognize means
    non-genuine. Trusted means an ``OpenAIChat`` or ``OpenAIResponses``
    instance that is not an OpenAI-compatible subclass (``OpenAILike``,
    ``OpenResponses`` — Azure, OpenRouter, DeepSeek, llama.cpp, and friends),
    with no instance ``base_url``, no ``client_params`` (agno merges those
    over the base client kwargs, so they can redirect the endpoint), no
    prebuilt or custom HTTP client, and no ``OPENAI_BASE_URL`` environment
    override (which the OpenAI SDK applies when no instance URL is set).
    Custom endpoints can serve arbitrary models under tiktoken-recognized
    ids, so only a provably genuine endpoint justifies trusting the model id
    to identify the serving tokenizer; genuine-OpenAI users who set
    ``client_params`` for timeouts and the like merely get the conservative
    byte-bound estimator.
    """
    if not isinstance_of_loaded(model, _OPENAI_CHAT_CLASS, _OPENAI_RESPONSES_CLASS):
        return False
    if isinstance_of_loaded(model, _OPENAI_LIKE_CLASS, _OPEN_RESPONSES_CLASS):
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
