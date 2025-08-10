#!/usr/bin/env python3
"""Alternative to screenshot - captures the widget's HTML state for viewing."""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def capture_widget_state() -> None:
    """Capture the current state of the widget configuration."""
    # Create output directory
    output_dir = Path(__file__).parent / "captures"
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")

    try:
        # Get the current configuration
        req = urllib.request.Request(
            "http://localhost:8000/api/config/load",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            config = json.loads(response.read().decode())

        # Save the configuration state
        state_file = output_dir / f"widget_state_{timestamp}.json"
        with state_file.open("w") as f:
            json.dump(config, f, indent=2)

        # Get list of agents
        with urllib.request.urlopen("http://localhost:8000/api/config/agents") as response:
            agents = json.loads(response.read().decode())

        # Get available tools
        with urllib.request.urlopen("http://localhost:8000/api/tools") as response:
            tools = json.loads(response.read().decode())

        # Get rooms
        with urllib.request.urlopen("http://localhost:8000/api/rooms") as response:
            rooms = json.loads(response.read().decode())

        # Create a summary
        summary = {
            "timestamp": timestamp,
            "agent_count": len(agents),
            "agents": [{"id": a["id"], "display_name": a["display_name"], "tools": len(a["tools"])} for a in agents],
            "available_tools": len(tools),
            "rooms": len(rooms),
            "models": list(config.get("models", {}).keys()),
        }

        summary_file = output_dir / f"widget_summary_{timestamp}.json"
        with summary_file.open("w") as f:
            json.dump(summary, f, indent=2)

        # Print a text representation
        for _agent in agents:
            pass

        for _model_id, _model_config in config.get("models", {}).items():
            pass

    except urllib.error.URLError:
        pass
    except Exception:  # noqa: S110
        pass


if __name__ == "__main__":
    capture_widget_state()
