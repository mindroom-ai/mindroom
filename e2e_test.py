#!/usr/bin/env python
"""Clean end-to-end test for Mindroom with memory system."""

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path

import nio
import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mindroom.cli import _run
from mindroom.matrix import MATRIX_HOMESERVER


class MindRoomE2ETest:
    """End-to-end test runner for Mindroom."""

    def __init__(self):
        self.client = None
        self.room_id = None
        self.username = None
        self.password = None

    async def setup(self):
        """Load credentials and setup client."""
        # Load user credentials
        with open("matrix_state.yaml") as f:
            users_data = yaml.safe_load(f)

        # Use the main user account
        self.username = users_data["accounts"]["user"]["username"]
        self.password = users_data["accounts"]["user"]["password"]
        self.room_id = users_data["rooms"]["lobby"]["room_id"]

        # Create client
        self.client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{self.username}:localhost")

    async def login(self):
        """Login to Matrix."""
        print(f"🔑 Logging in as {self.username}...")
        response = await self.client.login(self.password, device_name="e2e_test")
        if not isinstance(response, nio.LoginResponse):
            raise Exception(f"Failed to login: {response}")
        print("✓ Logged in successfully")

    async def send_mention(self, agent_name: str, message: str):
        """Send a message with proper Matrix mention."""
        user_id = f"@mindroom_{agent_name}:localhost"

        content = {"msgtype": "m.text", "body": f"{user_id} {message}", "m.mentions": {"user_ids": [user_id]}}

        response = await self.client.room_send(room_id=self.room_id, message_type="m.room.message", content=content)

        if isinstance(response, nio.RoomSendResponse):
            return response.event_id
        else:
            raise Exception(f"Failed to send message: {response}")

    async def get_recent_messages(self, limit=20):
        """Fetch recent messages from the room."""
        response = await self.client.room_messages(self.room_id, limit=limit)

        if not isinstance(response, nio.RoomMessagesResponse):
            raise Exception(f"Failed to fetch messages: {response}")

        messages = []
        for event in reversed(response.chunk):
            if isinstance(event, nio.RoomMessageText):
                sender = event.sender.split(":")[0].replace("@", "")
                messages.append({"sender": sender, "body": event.body, "timestamp": event.server_timestamp})
        return messages

    async def cleanup(self):
        """Close client connection."""
        if self.client:
            await self.client.close()

    def check_memory_storage(self, storage_path):
        """Check if memory storage was created."""
        chroma_path = storage_path / "chroma"
        if chroma_path.exists():
            files = list(chroma_path.rglob("*"))
            return True, len([f for f in files if f.is_file()])
        return False, 0


async def run_test_sequence(storage_path):
    """Run a complete test sequence."""
    test = MindRoomE2ETest()

    try:
        # Setup
        await test.setup()
        await test.login()

        print(f"\n📍 Testing in room: {test.room_id}")

        # Test cases
        test_cases = [
            {"agent": "calculator", "message": "what is 25 * 4?", "wait": 3, "description": "Basic calculation"},
            {
                "agent": "calculator",
                "message": "now add 50 to that result",
                "wait": 3,
                "description": "Follow-up using context",
            },
            {
                "agent": "general",
                "message": "what calculations were just performed?",
                "wait": 3,
                "description": "Cross-agent memory recall",
            },
            {
                "agent": "dev_team",
                "message": "write a simple hello world Python script",
                "wait": 8,
                "description": "Testing team coordination (DevTeam)",
            },
        ]

        # Run tests
        for i, test_case in enumerate(test_cases, 1):
            print(f"\n🧪 Test {i}: {test_case['description']}")
            print(f"   Agent: @{test_case['agent']}")
            print(f"   Message: {test_case['message']}")

            event_id = await test.send_mention(test_case["agent"], test_case["message"])
            print(f"   ✓ Sent (event_id: {event_id})")

            print(f"   ⏳ Waiting {test_case['wait']}s for response...")
            await asyncio.sleep(test_case["wait"])

        # Get results
        print("\n📊 Results:")
        messages = await test.get_recent_messages(limit=30)

        # Filter to show only recent relevant messages
        start_time = time.time() - 60  # Last minute
        recent = [m for m in messages if m["timestamp"] > start_time * 1000]

        print(f"\nLast {len(recent)} messages:")
        for msg in recent[-10:]:  # Show last 10
            sender = msg["sender"]
            body = msg["body"][:150] + "..." if len(msg["body"]) > 150 else msg["body"]

            # Format by sender type
            if sender.startswith("mindroom_"):
                print(f"  🤖 {sender}: {body}")
            else:
                print(f"  👤 {sender}: {body}")

        # Check memory
        print("\n💾 Memory System:")
        exists, file_count = test.check_memory_storage(storage_path)
        if exists:
            print(f"  ✓ ChromaDB storage created with {file_count} files")
        else:
            print("  ✗ No memory storage found")

    finally:
        await test.cleanup()


async def main():
    """Main entry point."""
    print("=" * 60)
    print("MINDROOM END-TO-END TEST")
    print("=" * 60)

    # Kill any existing mindroom processes
    print("\n🧹 Cleaning up old processes...")
    subprocess.run(["pkill", "-f", "mindroom run"], capture_output=True)
    await asyncio.sleep(2)

    # Start mindroom
    print("🚀 Starting Mindroom...")
    import tempfile

    temp_dir = tempfile.mkdtemp(prefix="mindroom_test_")
    bot_task = asyncio.create_task(_run(log_level="INFO", storage_path=Path(temp_dir)))

    # Wait for startup
    print("⏳ Waiting 15s for bot to start and sync...")
    await asyncio.sleep(15)

    # Run tests
    try:
        await run_test_sequence(Path(temp_dir))
        print("\n✅ Test completed successfully!")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # Cleanup
        print("\n🛑 Stopping bot...")
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task

        subprocess.run(["pkill", "-f", "mindroom run"], capture_output=True)

        # Clean up temp directory
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    # Run with: python e2e_test.py
    asyncio.run(main())
