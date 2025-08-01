#!/usr/bin/env python3
"""Demo script specifically for testing router agent behavior."""

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

    async def send_mention(
        self, room_id: str, user_id: str, agent_name: str, message: str, thread_id: str | None = None
    ) -> str:
        """Send a message with a mention."""
        content = {"msgtype": "m.text", "body": f"@{agent_name}: {message}", "m.mentions": {"user_ids": [user_id]}}

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
    """Run the router agent demo."""
    print("🚦 Router Agent Demo Script")
    print("=" * 50)
    print("This script tests the router agent behavior:")
    print("- Single agent in thread -> responds without mention")
    print("- Multiple agents in thread, none mentioned -> router decides")
    print("- Multiple agents mentioned -> all respond")
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
        print("📨 ROUTER AGENT TEST SCENARIOS")
        print("=" * 50)

        # Scenario 1: Start a thread (first agent should respond)
        print("\n1️⃣ Scenario: Starting a new thread")
        print("Expected: First agent to see message responds")
        thread_root = await client.send_message(room_id, "I need help calculating compound interest on my savings.")
        await wait_with_countdown(5, "Waiting for first agent")

        # Scenario 2: Continue thread (same agent should respond)
        print("\n2️⃣ Scenario: Continuing thread with single agent")
        print("Expected: Same agent continues conversation")
        await client.send_message(
            room_id, "The principal is $10,000 at 5% annual rate for 3 years.", thread_id=thread_root
        )
        await wait_with_countdown(5, "Waiting for response")

        # Scenario 3: New thread with a different topic
        print("\n3️⃣ Scenario: New thread with different topic")
        print("Expected: Different agent might respond based on topic")
        thread2_root = await client.send_message(room_id, "Can you help me write a Python function to sort a list?")
        await wait_with_countdown(5, "Waiting for agent")

        # Scenario 4: Continue second thread (testing single agent rule)
        print("\n4️⃣ Scenario: Continue second thread")
        print("Expected: Same agent continues")
        await client.send_message(room_id, "I want to sort by multiple keys.", thread_id=thread2_root)
        await wait_with_countdown(5, "Waiting for response")

        # Scenario 5: Mention an agent in first thread (should switch agents)
        if "agent_general" in agent_accounts:
            print("\n5️⃣ Scenario: Mention different agent in existing thread")
            print("Expected: Mentioned agent responds, now multiple agents in thread")
            await client.send_mention(
                room_id,
                "@mindroom_general:localhost",
                "general",
                "Can you explain compound interest in simple terms?",
                thread_id=thread_root,
            )
            await wait_with_countdown(5, "Waiting for general agent")

        # Scenario 6: Continue thread with multiple agents, no mention
        print("\n6️⃣ Scenario: Multiple agents in thread, no mention")
        print("Expected: Router agent decides who responds")
        await client.send_message(
            room_id, "What about if I compound monthly instead of annually?", thread_id=thread_root
        )
        await wait_with_countdown(7, "Waiting for router decision")

        # Scenario 7: Mention multiple agents
        if len(agent_accounts) >= 2:
            print("\n7️⃣ Scenario: Mention multiple agents in thread")
            print("Expected: All mentioned agents respond")

            # Create a new thread for clarity
            thread3_root = await client.send_message(
                room_id, "I have a complex question that needs multiple perspectives."
            )
            await wait_with_countdown(3, "Thread created")

            # Mention two agents
            agent_keys = list(agent_accounts.keys())[:2]
            mentions = []
            for agent_key in agent_keys:
                agent_display = agent_key.replace("agent_", "")
                mentions.append(f"@{agent_display}")

            message = f"{mentions[0]} and {mentions[1]}: How would you approach building a financial calculator app?"

            # Send with multiple mentions
            content = {
                "msgtype": "m.text",
                "body": message,
                "m.mentions": {
                    "user_ids": [
                        f"@{agent_accounts[agent_keys[0]].username}:localhost",
                        f"@{agent_accounts[agent_keys[1]].username}:localhost",
                    ]
                },
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": thread3_root,
                },
            }

            await client.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            print(f"📤 Sent: {message}")
            await wait_with_countdown(7, "Waiting for multiple responses")

        print("\n" + "=" * 50)
        print("✅ Router agent test completed!")
        print("Check your mindroom logs to verify:")
        print("- Single agents respond without mentions")
        print("- Router coordinates multi-agent threads")
        print("- Multiple mentions trigger multiple responses")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
