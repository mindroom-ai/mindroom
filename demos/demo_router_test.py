#!/usr/bin/env python3
"""Demo script specifically for testing router agent behavior with real Matrix messages."""

import asyncio
import os
import sys
from pathlib import Path

import nio
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mindroom.matrix_config import MatrixConfig

load_dotenv()
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")


class RouterTestClient:
    """Client for testing router agent behavior."""

    def __init__(self, username: str, password: str):
        self.client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{username}:localhost")
        self.password = password

    async def login(self):
        """Login to Matrix."""
        response = await self.client.login(self.password)
        if isinstance(response, nio.LoginResponse):
            print("✅ Logged in")
        else:
            raise Exception(f"Login failed: {response}")

    async def send_message(self, room_id: str, message: str, thread_id: str = None) -> str:
        """Send a message, optionally in a thread."""
        content = {"msgtype": "m.text", "body": message}

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        response = await self.client.room_send(room_id=room_id, message_type="m.room.message", content=content)

        if isinstance(response, nio.RoomSendResponse):
            print(f"📤 Sent: {message[:50]}...")
            return response.event_id
        else:
            raise Exception(f"Send failed: {response}")

    async def send_mention(self, room_id: str, agent_name: str, message: str, thread_id: str = None) -> str:
        """Send a message mentioning an agent."""
        user_id = f"@mindroom_{agent_name}:localhost"
        content = {"msgtype": "m.text", "body": f"@{agent_name}: {message}", "m.mentions": {"user_ids": [user_id]}}

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        response = await self.client.room_send(room_id=room_id, message_type="m.room.message", content=content)

        if isinstance(response, nio.RoomSendResponse):
            print(f"📤 Mentioned {agent_name}: {message[:50]}...")
            return response.event_id
        else:
            raise Exception(f"Mention failed: {response}")

    async def close(self):
        """Close the client."""
        await self.client.close()


async def wait_for_responses(seconds: int):
    """Wait with countdown."""
    print(f"⏳ Waiting {seconds}s for responses...", end="", flush=True)
    for _ in range(seconds):
        await asyncio.sleep(1)
        print(".", end="", flush=True)
    print(" ✓")


async def main():
    """Run router agent test scenarios."""
    print("🚦 Router Agent Test - Real Matrix Messages")
    print("=" * 60)

    # Load user credentials
    config = MatrixConfig.load()
    if "user" not in config.accounts:
        print("❌ No user account found. Please run: mindroom user create")
        return

    user_account = config.get_account("user")
    client = RouterTestClient(user_account.username, user_account.password)

    # Get room ID
    room_id = input("\nEnter room ID (e.g., !XEGXEOrxwHPgnroazx:localhost): ").strip()
    if not room_id.startswith("!"):
        print("❌ Invalid room ID")
        return

    try:
        await client.login()
        await client.client.join(room_id)

        print("\n" + "=" * 60)
        print("🧪 ROUTER AGENT TEST SCENARIOS")
        print("=" * 60)

        # Scenario 1: Start thread (first agent should respond)
        print("\n1️⃣ Starting new thread - expect first agent to respond")
        thread1 = await client.send_message(
            room_id, "I need help with financial calculations for my investment portfolio."
        )
        await wait_for_responses(5)

        # Scenario 2: Continue thread (same agent should respond)
        print("\n2️⃣ Continue thread - same agent should respond")
        await client.send_message(
            room_id, "The initial investment is $50,000 at 6% annual interest.", thread_id=thread1
        )
        await wait_for_responses(5)

        # Scenario 3: Mention different agent in thread
        print("\n3️⃣ Mention different agent - creates multi-agent thread")
        await client.send_mention(
            room_id, "general", "Can you explain investment strategies in simple terms?", thread_id=thread1
        )
        await wait_for_responses(5)

        # Scenario 4: No mentions in multi-agent thread - ROUTER SHOULD ACTIVATE
        print("\n4️⃣ No mentions in multi-agent thread - ROUTER SHOULD DECIDE")
        await client.send_message(
            room_id, "What's the difference between compound and simple interest?", thread_id=thread1
        )
        await wait_for_responses(8)  # Give router time to process

        # Scenario 5: Start new thread with different topic
        print("\n5️⃣ New thread with coding topic")
        await client.send_message(room_id, "I need help writing a Python script to parse CSV files.")
        await wait_for_responses(5)

        print("\n" + "=" * 60)
        print("✅ Router test scenarios completed!")
        print("\nCheck mindroom logs for router activity:")
        print("  grep 'Router:' ~/.local/share/mindroom/logs/*.log")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
