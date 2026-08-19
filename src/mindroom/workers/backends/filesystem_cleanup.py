"""Public descriptor-safe filesystem cleanup shared across worker-owned resources."""

from mindroom.workers.backends._metadata_store import remove_directory_tree_at as _remove_directory_tree_at


def remove_directory_tree_at(parent_fd: int, name: str) -> None:
    """Remove one descriptor-bound child tree without following symlinks."""
    _remove_directory_tree_at(parent_fd, name)
