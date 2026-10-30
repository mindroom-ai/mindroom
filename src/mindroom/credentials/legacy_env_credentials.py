"""Credential documents earlier releases copied from the environment into the primary's store."""

# LEGACY_COMPAT: Credentials copied from the environment into the primary's store.
# Legacy format: `<service>_credentials.json` in the primary's `credentials/` store holding `_source: "env"`; every start copied provider keys, `OLLAMA_HOST`, `GITHUB_TOKEN`, `EMBEDDER_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, and credential seeds there and never removed a copy whose variable was gone.
# Last legacy release: v2026.10.713; replacement: the next release reads them from the environment as a layer under the saved credentials and stores no copy.
# Handling: whenever the primary opens its store, delete every such document, so a variable removed from the environment stops supplying its credential; nothing writes `_source: "env"` any more, so later opens find none and need no receipt.
# An unreadable document stays, reads as no credential as the import treated it, and is retried at the next open; each removed service the environment no longer declares is logged by name.
# Coverage: tests/test_credentials_environment.py::test_a_key_removed_from_the_environment_stops_resolving_after_restart,
# tests/test_credentials_environment.py::test_an_unreadable_copy_is_kept_until_an_open_can_read_it,
# tests/test_credentials_environment.py::test_a_dashboard_save_survives_the_cleanup.

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection

    from mindroom.credentials.manager import CredentialsManager

logger = get_logger(__name__)


def remove_environment_copies(manager: CredentialsManager, *, declared_services: Collection[str]) -> None:
    """Delete the copies earlier releases imported from the environment."""
    removal = manager.remove_saved_documents(lambda credentials: credentials.get("_source") == "env")
    undeclared = sorted(set(removal.removed) - set(declared_services))
    if undeclared:
        logger.warning(
            "Removed credentials an earlier release copied from variables the environment no longer sets",
            services=undeclared,
        )
    if removal.unreadable:
        logger.warning(
            "Some stored credentials could not be read to remove environment copies; retrying at the next start",
            unreadable=removal.unreadable,
        )
