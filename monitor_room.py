#!/usr/bin/env python3
"""Simple room monitor to watch agent activity in real-time."""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import nio
from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.mindroom.matrix_config import MatrixConfig

load_dotenv()
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


async def monitor_room(room_id: str):
    """Monitor a room and display all messages."""
    config = MatrixConfig.load()
    if "user" not in config.accounts:
        print("❌ No user account found. Please run: mindroom user create")
        return

    user_account = config.get_account("user")
    client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{user_account.username}:localhost")

    # Login
    response = await client.login(user_account.password)
    if not isinstance(response, nio.LoginResponse):
        print(f"❌ Login failed: {response}")
        return

    print(f"✅ Logged in as {user_account.username}")
    print(f"👀 Monitoring room: {room_id}")
    print("-" * 80)

    # Join room
    await client.join(room_id)

    # Message callback
    def on_message(room: nio.MatrixRoom, event: nio.RoomMessageText):
        timestamp = datetime.now().strftime("%H:%M:%S")
        sender_name = event.sender.split(":")[0].replace("@", "")

        # Color code by sender type
        if sender_name.startswith("mindroom_") and sender_name != "mindroom_user":
            # Agent message
            agent = sender_name.replace("mindroom_", "")
            prefix = f"🤖 {agent}"
            color = "\033[32m"  # Green
        elif sender_name == "mindroom_user" or event.sender == client.user_id:
            # Demo user
            prefix = "👤 user"
            color = "\033[36m"  # Cyan
        else:
            # Other user
            prefix = f"👥 {sender_name}"
            color = "\033[33m"  # Yellow

        # Check if it's a thread reply
        relates_to = event.source.get("content", {}).get("m.relates_to", {})
        is_thread = relates_to.get("rel_type") == "m.thread"
        thread_marker = " [thread]" if is_thread else ""

        # Format message
        message = event.body
        if len(message) > 100:
            message = message[:97] + "..."

        print(f"{timestamp} {color}{prefix}{thread_marker}\033[0m: {message}")

    # Register callback
    client.add_event_callback(on_message, nio.RoomMessageText)

    print("\nMonitoring... Press Ctrl+C to stop\n")

    try:
        # Initial sync
        await client.sync(timeout=30000, full_state=True)

        # Keep syncing
        while True:
            await client.sync(timeout=30000)
    except KeyboardInterrupt:
        print("\n\n✋ Stopped monitoring")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ./monitor_room.py <room_id>")
        print("Example: ./monitor_room.py !XEGXEOrxwHPgnroazx:localhost")
        sys.exit(1)

    room_id = sys.argv[1]
    asyncio.run(monitor_room(room_id))
