"""Check a frozen helper's startup, status response, and EOF shutdown."""

# ruff: noqa: INP001

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def main() -> None:
    """Run the packaged executable with fresh, unpaired local state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper_app", type=Path)
    parser.add_argument("architecture", choices=("arm64", "x86_64"))
    args = parser.parse_args()
    executable = args.helper_app / "Contents/MacOS/MindRoom Desktop Helper"
    request_id = str(uuid4())
    request = {"v": 1, "request_id": request_id, "action": "status", "parameters": {}}
    with tempfile.TemporaryDirectory(prefix="mindroom-helper-smoke-") as directory:
        state = Path(directory)
        result = subprocess.run(
            [
                "/usr/bin/arch",
                f"-{args.architecture}",
                str(executable),
                "--config",
                str(state / "config.yaml"),
                "--storage-path",
                str(state / "storage"),
            ],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if result.returncode:
        message = f"{args.architecture} helper exited with {result.returncode}:\n{result.stderr}"
        raise SystemExit(message)
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages, result.stdout
    assert messages[0]["type"] == "hello", result.stdout
    assert messages[0]["protocol_version"] == 1, messages[0]
    response = next(message for message in messages if message.get("request_id") == request_id)
    assert response["ok"] is True, response
    assert response["result"]["status"]["config"]["state"] == "missing", response
    assert response["result"]["status"]["bridge"]["state"] == "stopped", response
    for permission in ("accessibility", "screen_recording"):
        assert response["result"]["status"]["permissions"][permission]["state"] in {"granted", "missing"}, response
    print(f"Verified {args.architecture} helper startup, status, and clean EOF shutdown.")


if __name__ == "__main__":
    main()
