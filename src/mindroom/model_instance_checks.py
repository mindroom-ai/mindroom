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


def isinstance_of_loaded(instance: object, *class_paths: tuple[str, str]) -> bool:
    """Return isinstance against the named classes, treating unloaded modules as no-match."""
    for module_name, class_name in class_paths:
        module = sys.modules.get(module_name)
        loaded_class = getattr(module, class_name, None) if module is not None else None
        if loaded_class is not None and isinstance(instance, loaded_class):
            return True
    return False


def is_genuine_openai_endpoint(model: object) -> bool:
    """Return whether requests for this model reach the real OpenAI API.

    OpenAI-compatible endpoints — ``OpenAILike`` subclasses such as Azure,
    OpenRouter, DeepSeek, and llama.cpp, an instance ``base_url`` override,
    or an ``OPENAI_BASE_URL`` environment override that the OpenAI SDK picks
    up when no instance URL is set — can serve arbitrary models under
    tiktoken-recognized ids, so only the genuine endpoint justifies trusting
    the model id to identify the serving tokenizer.
    """
    if not isinstance_of_loaded(model, _OPENAI_CHAT_CLASS, _OPENAI_RESPONSES_CLASS):
        return False
    if isinstance_of_loaded(model, _OPENAI_LIKE_CLASS):
        return False
    base_url = cast("OpenAIChat | OpenAIResponses", model).base_url
    return not base_url and not os.environ.get("OPENAI_BASE_URL")
