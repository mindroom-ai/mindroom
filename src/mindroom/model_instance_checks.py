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

import sys


def isinstance_of_loaded(instance: object, *class_paths: tuple[str, str]) -> bool:
    """Return isinstance against the named classes, treating unloaded modules as no-match."""
    for module_name, class_name in class_paths:
        module = sys.modules.get(module_name)
        loaded_class = getattr(module, class_name, None) if module is not None else None
        if loaded_class is not None and isinstance(instance, loaded_class):
            return True
    return False
