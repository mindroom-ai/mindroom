"""Compare isolated publication probes and semantic-search preparation overhead.

Run with ``uv run scripts/benchmark_runtime_preparation.py``.
Uses disposable synthetic data and a fixed-delay fake embedder; no providers,
Matrix accounts, live indexes, or credentials are used.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from dataclasses import dataclass

from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.config import Settings

from mindroom.knowledge.read_process import read_chroma
from mindroom.knowledge.read_protocol import ReadRequest
from mindroom.knowledge.read_proxy import ChromaReadProxy, collection_exists

_SAMPLES = 5


@dataclass
class _DelayedEmbedder(Embedder):
    def get_embedding(self, text: str) -> list[float]:
        del text
        time.sleep(0.25)
        return [1.0, 0.0]

    async def async_get_embedding(self, text: str) -> list[float]:
        del text
        await asyncio.sleep(0.25)
        return [1.0, 0.0]


async def main() -> None:
    """Print interleaved before/after timings as JSON."""
    samples: dict[str, list[float]] = {
        name: [] for name in ("native_exists", "metadata_exists", "serial_search", "overlapped_search")
    }
    with tempfile.TemporaryDirectory(prefix="mindroom-preparation-benchmark-") as index_path:
        with Client(settings=Settings(is_persistent=True, persist_directory=index_path)) as client:
            collection = client.create_collection("benchmark")
            collection.add(ids=["known"], embeddings=[[1.0, 0.0]], documents=["known result"])
        proxy = ChromaReadProxy("benchmark", index_path, _DelayedEmbedder())
        for _ in range(_SAMPLES):
            started = time.monotonic()
            assert (await asyncio.to_thread(read_chroma, ReadRequest(index_path, "benchmark"))).exists
            samples["native_exists"].append((time.monotonic() - started) * 1000)
            started = time.monotonic()
            assert await asyncio.to_thread(collection_exists, index_path, "benchmark")
            samples["metadata_exists"].append((time.monotonic() - started) * 1000)
            started = time.monotonic()
            serial = await asyncio.to_thread(proxy.search, "query")
            samples["serial_search"].append((time.monotonic() - started) * 1000)
            started = time.monotonic()
            overlapped = await proxy.async_search("query")
            samples["overlapped_search"].append((time.monotonic() - started) * 1000)
            assert (
                [document.content for document in serial]
                == [document.content for document in overlapped]
                == [
                    "known result",
                ]
            )
    print(
        json.dumps(
            {
                "samples": _SAMPLES,
                "synthetic_embedding_delay_ms": 250,
                "timings": {
                    name: {
                        "median_ms": round(statistics.median(values), 3),
                        "samples_ms": [round(v, 3) for v in values],
                    }
                    for name, values in samples.items()
                },
            },
            indent=2,
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
