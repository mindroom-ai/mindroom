"""Backward compatibility module for thread information utilities.

This module has been replaced by event_relations.py which provides more
comprehensive event relation analysis. The exports here are maintained
for backward compatibility.

DEPRECATED: Use mindroom.matrix.event_relations instead.
"""

from .event_relations import EventRelationInfo as ThreadInfo
from .event_relations import analyze_event_relations as analyze_thread_info

__all__ = ["ThreadInfo", "analyze_thread_info"]
