"""Retained marker for the superseded public-sync checkpoint owner.

The active managed-bot path now obtains authenticated source positions and
room continuity from the private owned ingestion journal. This module stays
present—but deliberately has no executable checkpoint machinery—during the
Task 6 observation interval, where external coverage must confirm zero use.
"""

__all__: tuple[str, ...] = ()
