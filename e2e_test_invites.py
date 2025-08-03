#!/usr/bin/env python
"""End-to-end test for agent invitation features."""

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path

import nio

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mindroom.matrix import MATRIX_HOMESERVER


class InviteE2ETest:
    """End-to-end test for invitation features."""

    def __init__(self):
        self.client = None
        self.lobby_room_id = None
        self.science_room_id = None
        self.username = None
        self.password = None

    async def setup(self):
        """Load credentials and setup client."""
        # Create a test user with consistent credentials
        self.username = "e2e_test_user"
        self.password = "e2e_test_password_12345"

        # Create client
        self.client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{self.username}:localhost")

        # Try to register the test user
        response = await self.client.register(
            username=self.username, password=self.password, device_name="e2e_test_device"
        )
        if isinstance(response, nio.RegisterResponse):
            print(f"✅ Registered test user: @{self.username}:localhost")
            # Set access token from registration
            self.client.access_token = response.access_token
            self.client.device_id = response.device_id
        elif (
            isinstance(response, nio.ErrorResponse)
            and hasattr(response, "status_code")
            and response.status_code == "M_USER_IN_USE"
        ):
            print("ℹ️  Test user already exists, will try to login")
        else:
            print(f"⚠️  Registration response: {response}")

        # We'll set room IDs after mindroom creates them
        self.lobby_room_id = None
        self.science_room_id = None

    async def login(self):
        """Login to Matrix."""
        # Skip login if we already have access token from registration
        if not self.client.access_token:
            print(f"🔑 Logging in as {self.username}...")
            response = await self.client.login(self.password, device_name="e2e_invite_test")
            if not isinstance(response, nio.LoginResponse):
                raise Exception(f"Failed to login: {response}")
            print("✓ Logged in successfully")
        else:
            print("✓ Already authenticated from registration")

    async def discover_rooms(self):
        """Discover room IDs by joining public rooms."""
        print("🔍 Discovering rooms...")

        # Join lobby room
        lobby_alias = "#lobby:localhost"
        response = await self.client.join(lobby_alias)
        if isinstance(response, nio.JoinResponse):
            self.lobby_room_id = response.room_id
            print(f"✓ Joined lobby: {self.lobby_room_id}")
        else:
            print(f"⚠️  Failed to join lobby: {response}")

        # Join science room
        science_alias = "#science:localhost"
        response = await self.client.join(science_alias)
        if isinstance(response, nio.JoinResponse):
            self.science_room_id = response.room_id
            print(f"✓ Joined science: {self.science_room_id}")
        else:
            print(f"⚠️  Failed to join science: {response}")

    async def send_message(self, room_id: str, message: str, thread_id: str = None):
        """Send a plain message or thread reply."""
        content = {
            "msgtype": "m.text",
            "body": message,
        }

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        response = await self.client.room_send(room_id=room_id, message_type="m.room.message", content=content)

        if isinstance(response, nio.RoomSendResponse):
            return response.event_id
        else:
            raise Exception(f"Failed to send message: {response}")

    async def send_mention(self, room_id: str, agent_name: str, message: str, thread_id: str = None):
        """Send a message with proper Matrix mention."""
        from mindroom.matrix import MatrixID

        user_id = MatrixID.from_agent(agent_name, "localhost").full_id

        content = {"msgtype": "m.text", "body": f"{user_id} {message}", "m.mentions": {"user_ids": [user_id]}}

        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
            }

        response = await self.client.room_send(room_id=room_id, message_type="m.room.message", content=content)

        if isinstance(response, nio.RoomSendResponse):
            return response.event_id
        else:
            raise Exception(f"Failed to send message: {response}")

    async def get_thread_messages(self, room_id: str, thread_id: str, limit=20):
        """Fetch messages from a specific thread."""
        # Note: In a real implementation, we'd filter by thread relation
        # For now, we'll get recent messages and filter in post-processing
        response = await self.client.room_messages(room_id, limit=limit)

        if not isinstance(response, nio.RoomMessagesResponse):
            raise Exception(f"Failed to fetch messages: {response}")

        messages = []
        for event in reversed(response.chunk):
            if isinstance(event, nio.RoomMessageText):
                # Check if it's part of the thread
                relates_to = event.source.get("content", {}).get("m.relates_to", {})
                if relates_to.get("event_id") == thread_id and relates_to.get("rel_type") == "m.thread":
                    from mindroom.matrix import MatrixID

                    sender_id = MatrixID.parse(event.sender)
                    sender = sender_id.username
                    messages.append(
                        {
                            "sender": sender,
                            "body": event.body,
                            "timestamp": event.server_timestamp,
                            "event_id": event.event_id,
                        }
                    )
        return messages

    async def get_recent_messages(self, room_id: str, limit=20):
        """Fetch recent messages from a room."""
        response = await self.client.room_messages(room_id, limit=limit)

        if not isinstance(response, nio.RoomMessagesResponse):
            raise Exception(f"Failed to fetch messages: {response}")

        messages = []
        for event in reversed(response.chunk):
            if isinstance(event, nio.RoomMessageText):
                from mindroom.matrix import MatrixID

                sender_id = MatrixID.parse(event.sender)
                sender = sender_id.username
                messages.append(
                    {
                        "sender": sender,
                        "body": event.body,
                        "timestamp": event.server_timestamp,
                        "event_id": event.event_id,
                    }
                )
        return messages

    async def cleanup(self):
        """Close client connection."""
        if self.client:
            await self.client.close()


async def test_thread_invitations(test):
    """Test thread-specific agent invitations."""
    print("\n🧪 Testing Thread Invitations")
    print("=" * 40)

    # Start a thread in lobby
    print("\n1️⃣ Starting a thread in lobby room...")
    thread_id = await test.send_mention(test.lobby_room_id, "general", "I need help with some calculations")
    print(f"   ✓ Thread started (id: {thread_id})")
    await asyncio.sleep(3)

    # Try to invite calculator (which is in science room) to this thread
    print("\n2️⃣ Inviting calculator agent to thread...")
    await test.send_message(test.lobby_room_id, "/invite calculator", thread_id=thread_id)
    await asyncio.sleep(3)

    # Ask calculator a question in the thread
    print("\n3️⃣ Asking calculator in thread...")
    await test.send_mention(test.lobby_room_id, "calculator", "what is 123 * 456?", thread_id=thread_id)
    await asyncio.sleep(5)

    # Check thread messages
    print("\n4️⃣ Checking thread responses...")
    messages = await test.get_recent_messages(test.lobby_room_id, limit=30)

    # Filter for thread messages
    thread_messages = []
    for msg in messages:
        if "123 * 456" in msg["body"] or "56088" in msg["body"] or "/invite" in msg["body"] or "Invited" in msg["body"]:
            thread_messages.append(msg)

    print(f"\n   Found {len(thread_messages)} relevant messages:")
    for msg in thread_messages[-10:]:  # Show last 10
        sender = msg["sender"]
        body = msg["body"][:150] + "..." if len(msg["body"]) > 150 else msg["body"]
        if sender.startswith("mindroom_"):
            print(f"   🤖 {sender}: {body}")
        else:
            print(f"   👤 {sender}: {body}")

    # List invites
    print("\n5️⃣ Listing thread invites...")
    await test.send_message(test.lobby_room_id, "/list_invites", thread_id=thread_id)
    await asyncio.sleep(3)

    return thread_id


async def test_no_response_outside_threads(test):
    """Test that agents don't respond outside threads."""
    print("\n\n🧪 Testing No Response Outside Threads")
    print("=" * 40)

    # Try to mention an agent in main room (not in thread)
    print("\n1️⃣ Mentioning calculator in main room (not in thread)...")
    await test.send_mention(test.lobby_room_id, "calculator", "what is 2+2?")
    await asyncio.sleep(5)

    # Check room messages
    print("\n2️⃣ Checking that no response was sent...")
    messages = await test.get_recent_messages(test.lobby_room_id, limit=10)

    recent = [m for m in messages if m["timestamp"] > (time.time() - 30) * 1000]
    from mindroom.matrix import MatrixID

    calculator_responded = any(m["sender"] == MatrixID.from_agent("calculator", "localhost").full_id for m in recent)

    if calculator_responded:
        print("   ❌ ERROR: Calculator responded outside of thread!")
    else:
        print("   ✅ Correct: Calculator did not respond outside thread")

    # Try invite command outside thread
    print("\n3️⃣ Trying invite command outside thread...")
    await test.send_message(test.lobby_room_id, "/invite research")
    await asyncio.sleep(3)

    messages = await test.get_recent_messages(test.lobby_room_id, limit=5)
    invite_error = any("only work" in m["body"] and "thread" in m["body"] for m in messages)

    if invite_error:
        print("   ✅ Correct: Got error message about threads")
    else:
        print("   ❌ ERROR: No error message about thread requirement")


async def test_help_command(test):
    """Test help command."""
    print("\n\n🧪 Testing Help Command")
    print("=" * 40)

    print("\n1️⃣ Getting general help...")
    await test.send_message(test.lobby_room_id, "/help")
    await asyncio.sleep(3)

    print("\n2️⃣ Getting invite command help...")
    await test.send_message(test.lobby_room_id, "/help invite")
    await asyncio.sleep(3)

    # Check messages
    messages = await test.get_recent_messages(test.lobby_room_id, limit=10)
    help_messages = [m for m in messages if "Available Commands" in m["body"] or "Invite Command" in m["body"]]

    print(f"\n   Found {len(help_messages)} help messages")
    for msg in help_messages[-2:]:
        print("\n   📖 Help response preview:")
        print(f"      {msg['body'][:200]}...")


async def run_test_sequence():
    """Run complete invitation test sequence."""
    test = InviteE2ETest()

    try:
        # Setup
        await test.setup()
        await test.login()
        await test.discover_rooms()

        print("\n📍 Testing in rooms:")
        print(f"   - Lobby: {test.lobby_room_id}")
        print(f"   - Science: {test.science_room_id}")

        if not test.lobby_room_id or not test.science_room_id:
            print("❌ Failed to discover rooms. Make sure mindroom has created them.")
            return

        # Run test sequences
        await test_thread_invitations(test)
        await test_no_response_outside_threads(test)
        await test_help_command(test)

        print("\n\n✅ All invitation tests completed!")

    finally:
        await test.cleanup()


async def main():
    """Main entry point."""
    print("=" * 60)
    print("MINDROOM INVITATION FEATURE E2E TEST")
    print("=" * 60)

    # Kill any existing mindroom processes
    print("\n🧹 Cleaning up old processes...")
    subprocess.run(["pkill", "-f", "mindroom run"], capture_output=True)
    await asyncio.sleep(2)

    # Start mindroom
    print("🚀 Starting Mindroom...")
    import tempfile

    from mindroom.cli import _run

    temp_dir = tempfile.mkdtemp(prefix="mindroom_invite_test_")
    bot_task = asyncio.create_task(_run(log_level="INFO", storage_path=Path(temp_dir)))

    # Wait for startup
    print("⏳ Waiting 15s for bot to start, create rooms, and sync...")
    await asyncio.sleep(15)

    # Run tests
    try:
        await run_test_sequence()
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
    # Run with: python e2e_test_invites.py
    asyncio.run(main())
