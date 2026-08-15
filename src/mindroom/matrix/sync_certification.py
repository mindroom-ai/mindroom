"""Retained marker for the superseded public-sync certification engine.

Managed MindRoom bots consume the private owned ingestion stream. Cursor,
record, and frame durability therefore belong to ``nio.ingest`` and the
MindRoom event journal rather than to a second response-level certifier. The
module remains intentionally inert through the Task 6 observation window so
external coverage can prove that no production path enters the old engine.
"""

__all__: tuple[str, ...] = ()
