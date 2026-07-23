"""Azure OpenAI model with cross-provider tool-call replay support.

Kept out of :mod:`mindroom.openai_models`: importing
``agno.models.azure.openai_chat`` runs the ``agno.models.azure`` package init,
which imports its Claude model and with it the anthropic SDK (~200 ms).
Only the ``azure`` provider branch should pay that.
"""

from __future__ import annotations

from dataclasses import dataclass

# The concrete module, not agno.models.azure: the package init wraps
# this import in a try/except stub whose fallback is not a Model.
from agno.models.azure.openai_chat import AzureOpenAI

from mindroom.openai_models import ChatToolArgumentsCompat


@dataclass
class MindRoomAzureOpenAI(ChatToolArgumentsCompat, AzureOpenAI):
    """Azure OpenAI model that can replay tool calls from other providers."""
