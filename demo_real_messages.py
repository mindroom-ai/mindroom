#!/usr/bin/env python3
"""Real demo script that sends messages to Matrix server and displays responses.

This script sends various types of messages to test agent responses:
- Direct mentions
- Thread messages
- Multi-agent scenarios
"""

import asyncio
import os
import sys
from pathlib import Path

import nio
from dotenv import load_dotenv

# Add src to path so we can import mindroom modules
sys.path.insert(0, str(Path(__file__).parent))

from src.mindroom.matrix_config import MatrixConfig

# Load environment variables
load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


class DemoClient:
    """Demo client for sending test messages."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{username}:localhost")

    async def login(self) -> None:
        """Login to Matrix server."""
        response = await self.client.login(self.password)
        if isinstance(response, nio.LoginResponse):
            print(f"✅ Logged in as {self.username}")
        else:
            print(f"❌ Failed to login: {response}")
            raise Exception(f"Login failed: {response}")

    async def join_room(self, room_id: str) -> None:
        """Join a room."""
        response = await self.client.join(room_id)
        if isinstance(response, nio.JoinResponse):
            print(f"✅ Joined room {room_id}")
        else:
            print(f"❌ Failed to join room: {response}")

    async def send_message(self, room_id: str, message: str, thread_id: str | None = None) -> str:
        """Send a message to a room."""
        content = {
            "msgtype": "m.text",
            "body": message,
        }

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(response, nio.RoomSendResponse):
            print(f"📤 Sent: {message}")
            return response.event_id
        else:
            print(f"❌ Failed to send message: {response}")
            raise Exception(f"Send failed: {response}")

    async def send_mention(self, room_id: str, user_id: str, agent_name: str, message: str) -> str:
        """Send a message with a mention."""
        content = {"msgtype": "m.text", "body": f"@{agent_name}: {message}", "m.mentions": {"user_ids": [user_id]}}

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(response, nio.RoomSendResponse):
            print(f"📤 Sent mention to {agent_name}: {message}")
            return response.event_id
        else:
            print(f"❌ Failed to send mention: {response}")
            raise Exception(f"Send failed: {response}")

    async def close(self) -> None:
        """Close the client."""
        await self.client.close()


async def wait_with_countdown(seconds: int, message: str) -> None:
    """Wait with a countdown display."""
    print(f"\n⏳ {message}", end="", flush=True)
    for i in range(seconds, 0, -1):
        print(f" {i}", end="", flush=True)
        await asyncio.sleep(1)
    print(" ✓")


async def main():
    """Run the demo."""
    print("🚀 Matrix Message Demo Script")
    print("=" * 50)
    print("This script will send real messages to your Matrix server.")
    print("Watch the mindroom logs to see agent responses!")
    print("=" * 50)

    # Load user credentials
    config = MatrixConfig.load()
    if "user" not in config.accounts:
        print("❌ No mindroom_user found in matrix_users.yaml")
        print("Please run: mindroom user create")
        return

    user_account = config.get_account("user")
    client = DemoClient(user_account.username, user_account.password)

    # Get room ID from user
    print("\nEnter the room ID where agents are active")
    print("(e.g., !XEGXEOrxwHPgnroazx:localhost)")
    room_id = input("Room ID: ").strip()

    if not room_id.startswith("!"):
        print("❌ Invalid room ID. Should start with !")
        return

    try:
        # Login and join room
        await client.login()
        await client.join_room(room_id)

        # Load agent users to get their user IDs
        agent_accounts = {key: acc for key, acc in config.accounts.items() if key.startswith("agent_")}

        print(f"\n📋 Found {len(agent_accounts)} agents:")
        for agent_key in agent_accounts:
            display_name = agent_key.replace("agent_", "")
            print(f"  - {display_name}")

        print("\n" + "=" * 50)
        print("📨 DEMO: Sending test messages...")
        print("=" * 50)

        # Demo 1: Direct mention to calculator agent
        if "agent_calculator" in agent_accounts:
            print("\n1️⃣ Direct mention to calculator agent:")
            await client.send_mention(room_id, "@mindroom_calculator:localhost", "calculator", "What is 25 * 4?")
            await wait_with_countdown(3, "Waiting for response")

        # Demo 2: Direct mention to general agent
        if "agent_general" in agent_accounts:
            print("\n2️⃣ Direct mention to general agent:")
            await client.send_mention(
                room_id, "@mindroom_general:localhost", "general", "Tell me a fun fact about Python!"
            )
            await wait_with_countdown(3, "Waiting for response")

        # Demo 3: Start a thread
        print("\n3️⃣ Starting a thread conversation:")
        thread_root = await client.send_message(room_id, "Let's discuss mathematics in this thread!")
        await wait_with_countdown(2, "Thread created")

        # Demo 4: Reply in thread (should trigger only one agent)
        print("\n4️⃣ Sending message in thread:")
        await client.send_message(room_id, "What is the square root of 144?", thread_id=thread_root)
        await wait_with_countdown(3, "Waiting for response")

        # Demo 5: Multiple mentions in thread
        if len(agent_accounts) >= 2:
            print("\n5️⃣ Mentioning multiple agents in thread:")
            agent_keys = list(agent_accounts.keys())[:2]

            # First agent
            agent1_display = agent_keys[0].replace("agent_", "")
            agent1_username = agent_accounts[agent_keys[0]].username
            await client.send_mention(
                room_id, f"@{agent1_username}:localhost", agent1_display, "Can you help with this calculation?"
            )
            await wait_with_countdown(2, "Waiting")

            # Second agent
            agent2_display = agent_keys[1].replace("agent_", "")
            agent2_username = agent_accounts[agent_keys[1]].username
            await client.send_mention(
                room_id, f"@{agent2_username}:localhost", agent2_display, "What do you think about this problem?"
            )
            await wait_with_countdown(3, "Waiting for responses")

        # Demo 6: Regular message (no mention, no thread)
        print("\n6️⃣ Regular message (should be ignored):")
        await client.send_message(room_id, "This message has no mentions and is not in a thread.")
        await wait_with_countdown(2, "Checking for responses")

        print("\n" + "=" * 50)
        print("✅ Demo completed!")
        print("Check your mindroom logs to see how agents responded.")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
