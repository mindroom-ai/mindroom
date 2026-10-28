"""Contextual retrieval: a model writes a short context for each chunk, which is prepended before indexing."""

from __future__ import annotations

import asyncio
from itertools import accumulate
from typing import TYPE_CHECKING

from mindroom.agent_loop.helper import complete_prompt
from mindroom.helper_usage import record_system_usage
from mindroom.providers import registry as provider_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

#: Model calls one file keeps in flight.
_CONCURRENCY = 8
#: Document text sent with each chunk; a longer document sends the window around the chunk.
_MAX_DOCUMENT_CHARS = 60_000
# Adapted from Anthropic's contextual retrieval prompt (https://www.anthropic.com/news/contextual-retrieval). The
# document goes in the system message, ahead of the varying chunk, so providers can cache it across a file's chunks.
_DOCUMENT = "<document>\n{document}\n</document>"
_PROMPT = """Here is the chunk we want to situate within the whole document
<chunk>
{chunk}
</chunk>
Please give a short succinct context to situate this chunk within the overall document for the purposes of improving \
search retrieval of the chunk. Answer only with the succinct context and nothing else."""


def _document_window(document: str, start: int, length: int) -> str:
    """Return the whole document, or a window of ``_MAX_DOCUMENT_CHARS`` holding the chunk.

    Window starts snap to a quarter of the window, so neighbouring chunks send the same window and a provider's prompt
    cache can serve it to all of them.
    """
    if len(document) <= _MAX_DOCUMENT_CHARS:
        return document
    stride = _MAX_DOCUMENT_CHARS // 4
    centered = start + length // 2 - _MAX_DOCUMENT_CHARS // 2
    begin = min(max(centered // stride * stride, 0), len(document) - _MAX_DOCUMENT_CHARS)
    return document[begin : begin + _MAX_DOCUMENT_CHARS]


async def chunk_contexts(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
    chunks: Sequence[str],
) -> list[str]:
    """Return one short context per chunk of one document, in order; the first failed model call raises."""
    provider = provider_registry.build_model_provider(config, runtime_paths, model_name)
    document = "\n".join(chunks)
    starts = [0, *accumulate(len(chunk) + 1 for chunk in chunks[:-1])]
    slots = asyncio.Semaphore(_CONCURRENCY)

    async def context_for(index: int) -> str:
        chunk = chunks[index]
        async with slots:
            # Built inside the slot, so queued chunks hold no copy of the document.
            window = _document_window(document, starts[index], len(chunk))
            response = await complete_prompt(
                provider,
                _PROMPT.format(chunk=chunk),
                instructions=(_DOCUMENT.format(document=window),),
            )
        await record_system_usage(response.usage, runtime_paths=runtime_paths, kind="knowledge_chunk_context")
        return str(response.content).strip()

    # The first call runs alone, so providers that cache prompt prefixes can serve the shared document to the rest.
    first = await context_for(0)
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(context_for(index)) for index in range(1, len(chunks))]
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return [first, *(task.result() for task in tasks)]
