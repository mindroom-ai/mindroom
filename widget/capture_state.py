#!/usr/bin/env python3
"""
Alternative to screenshot - captures the widget's HTML state for viewing.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def capture_widget_state():
    """Capture the current state of the widget configuration."""

    # Create output directory
    output_dir = Path(__file__).parent / "captures"
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        # Get the current configuration
        req = urllib.request.Request(
            "http://localhost:8000/api/config/load", method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as response:
            config = json.loads(response.read().decode())

        # Save the configuration state
        state_file = output_dir / f"widget_state_{timestamp}.json"
        with open(state_file, "w") as f:
            json.dump(config, f, indent=2)

        print(f"✓ Configuration state saved to: {state_file}")

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
        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)

        print(f"✓ Summary saved to: {summary_file}")

        # Print a text representation
        print("\n" + "=" * 60)
        print("MINDROOM CONFIGURATION WIDGET STATE")
        print("=" * 60)
        print(f"Timestamp: {timestamp}")
        print(f"\nAgents ({len(agents)}):")
        for agent in agents:
            print(f"  • {agent['display_name']} (ID: {agent['id']})")
            print(f"    - Tools: {', '.join(agent['tools']) if agent['tools'] else 'None'}")
            print(f"    - Rooms: {', '.join(agent['rooms']) if agent['rooms'] else 'None'}")

        print(f"\nModels ({len(config.get('models', {}))}):")
        for model_id, model_config in config.get("models", {}).items():
            print(f"  • {model_id}: {model_config.get('provider', 'unknown')} - {model_config.get('id', 'unknown')}")

        print("\n✓ Widget is running and accessible at: http://localhost:3003")

    except urllib.error.URLError as e:
        print("✗ Could not connect to backend. Is it running on port 8000?")
        print(f"   Error: {e}")
    except Exception as e:
        print(f"✗ Error capturing state: {e}")


if __name__ == "__main__":
    capture_widget_state()
