"""Docker worker control records in the shapes earlier releases wrote."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from mindroom.durable_write import write_json_file_durable
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.worker_retirement import open_worker_state_root, read_worker_identity

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_LEGACY_METADATA_PATH = ("metadata", "worker.json")

# LEGACY_COMPAT: Docker worker lifecycle records inside the bind-mounted worker state root.
# Legacy format: `workers/<worker_dir>/metadata/worker.json`, selected at backend startup when the worker
# has no record under `workers/.mindroom-worker-control/<worker_dir>/metadata/worker.json`.
# Last legacy release: v2026.9.284; replacement: v2026.9.285, which keeps the record in the unmounted control
# directory.
# Handling: The old file was writable by the worker's own tool code, so only its `worker_key` is read,
# through bounded no-follow descriptors, and it is adopted only when `worker_dir_name(worker_key)` names the
# directory holding the file; that digest-bound name cannot be claimed for another worker's key.
# Every other field is discarded and the owner writes a fresh idle control record, so the worker is listed,
# idle-stopped, shut down, and retired again while its container is only addressed by the name derived from the key.
# Unverifiable records are skipped with a warning and the old file stays inert inside the worker's own root.
# Coverage: tests/test_docker_worker_backend.py::test_docker_backend_adopts_legacy_mounted_worker_records;
# tests/test_docker_worker_backend.py::test_docker_backend_skips_unverifiable_legacy_worker_records.


def legacy_docker_worker_keys(workers_root: Path, *, control_root: Path) -> list[str]:
    """Return verified keys of workers whose only lifecycle record sits inside their mounted state root."""
    if not workers_root.is_dir():
        return []
    worker_keys: list[str] = []
    with os.scandir(workers_root) as entries:
        candidates = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    for worker_name in candidates:
        if os.path.lexists(control_root / worker_name / "metadata" / "worker.json"):
            continue
        if not os.path.lexists(workers_root / worker_name / "metadata" / "worker.json"):
            continue
        try:
            with open_worker_state_root(workers_root, workers_subpath=(), worker_name=worker_name) as state:
                if state.worker_fd is None:
                    continue
                worker_key = read_worker_identity(
                    state.worker_fd,
                    identity_path=_LEGACY_METADATA_PATH,
                    identity_field_path=("worker_key",),
                )
        except (OSError, TypeError, ValueError):
            logger.warning("Skipping unreadable legacy Docker worker record in %r", worker_name, exc_info=True)
            continue
        if not isinstance(worker_key, str) or worker_dir_name(worker_key) != worker_name:
            logger.warning("Skipping legacy Docker worker record whose key does not own %r", worker_name)
            continue
        worker_keys.append(worker_key)
    return worker_keys


# LEGACY_COMPAT: Docker control records that copy the container's endpoint, port, ID, image, and launch hashes.
# Legacy format: `workers/.mindroom-worker-control/<worker_dir>/metadata/worker.json` holding any of `endpoint`,
# `backend_name`, `host_port`, `container_id`, `image`, `publish_host`, `worker_port`, `launch_config_hash`, or
# `startup_manifest_hash` beside the worker's identity and lifecycle keys.
# Last legacy release: v2026.10.713; replacement: the next release, whose records keep only `worker_id`,
# `worker_key`, `container_name`, and the lifecycle keys, and whose containers carry their launch hash as labels.
# Handling: When the Docker backend starts, before anything reads a record as its current type (which rejects
# unknown keys), each record holding a retired key is rewritten without it, keeping its identity and lifecycle
# history, so its worker stays listed, idle-stopped, and shut down.
# The control directory is never mounted into a worker, so the records are primary-owned; a record that is not a
# readable JSON object is left as it is and stays unreadable to the backend, as before.
# A container started by that release carries a launch hash of the older form, so its next ensure replaces it once.
# Coverage: tests/test_docker_worker_backend.py::test_docker_backend_drops_retired_keys_from_old_control_records.
_RETIRED_CONTROL_RECORD_KEYS = frozenset(
    {
        "endpoint",
        "backend_name",
        "host_port",
        "container_id",
        "image",
        "publish_host",
        "worker_port",
        "launch_config_hash",
        "startup_manifest_hash",
    },
)


def drop_retired_docker_worker_record_keys(control_root: Path) -> None:
    """Rewrite every control record that still holds a retired key without it."""
    if not control_root.is_dir():
        return
    for record_path in sorted(control_root.glob("*/metadata/worker.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or _RETIRED_CONTROL_RECORD_KEYS.isdisjoint(record):
            continue
        write_json_file_durable(
            record_path,
            {key: value for key, value in record.items() if key not in _RETIRED_CONTROL_RECORD_KEYS},
            strict_atomic_replace=True,
            sort_keys=True,
        )
