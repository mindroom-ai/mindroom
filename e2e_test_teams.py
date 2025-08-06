#!/usr/bin/env python3
"""End-to-end test for team collaboration feature."""

import asyncio
import contextlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import nio
import yaml
from dotenv import load_dotenv

# Add the src directory to the path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mindroom.cli import _run
from mindroom.matrix import MATRIX_HOMESERVER, matrix_client
from mindroom.matrix.mentions import create_mention_content_from_text

# Load environment variables
load_dotenv()


# Configuration - Load from matrix_state.yaml
def load_test_config():
    """Load current credentials and room IDs from matrix_state.yaml."""

    try:
        with open("matrix_state.yaml") as f:
            state = yaml.safe_load(f)

        user_account = state["accounts"]["user"]
        lobby_room = state["rooms"]["lobby"]

        return {
            "username": user_account["username"],
            "password": user_account["password"],
            "room_id": lobby_room["room_id"],
        }
    except Exception as e:
        print(f"❌ Failed to load matrix_state.yaml: {e}")
        return None


# Load current configuration
config = load_test_config()
if not config:
    print("❌ Cannot load configuration from matrix_state.yaml")
    sys.exit(1)

TEST_ROOM_ID = config["room_id"]  # Current lobby room with all agents
USERNAME = config["username"]
PASSWORD = config["password"]


async def send_message(client: nio.AsyncClient, room_id: str, message: str) -> str:
    """Send a message to a room and return the event ID."""
    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": message,
        },
    )
    if isinstance(response, nio.RoomSendResponse):
        return response.event_id
    else:
        print(f"Failed to send message: {response}")
        return ""


async def send_message_with_mentions(client: nio.AsyncClient, room_id: str, message: str) -> str:
    """Send a message with proper Matrix mentions and return the event ID."""
    content = create_mention_content_from_text(message)
    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )
    if isinstance(response, nio.RoomSendResponse):
        return response.event_id
    else:
        print(f"Failed to send message: {response}")
        return ""


async def get_recent_messages(client: nio.AsyncClient, room_id: str, limit: int = 10) -> list[dict]:
    """Get recent messages from a room."""
    response = await client.room_messages(room_id, start="", limit=limit)
    if not isinstance(response, nio.RoomMessagesResponse):
        return []

    messages = []
    for event in response.chunk:
        if isinstance(event, nio.RoomMessageText):
            sender = event.sender.split(":")[0][1:]  # Extract username
            body = event.body
            messages.append({"sender": sender, "body": body})

    return list(reversed(messages))


async def test_team_collaboration():
    """Test team collaboration scenarios."""
    print("🧪 TEAM COLLABORATION TEST")
    print("=" * 60)

    # Login
    print(f"🔑 Logging in as {USERNAME}...")
    print(f"   Homeserver: {MATRIX_HOMESERVER}")

    async with matrix_client(MATRIX_HOMESERVER, f"@{USERNAME}:localhost") as client:
        print("   Client created successfully")
        login_response = await client.login(PASSWORD)
        if not isinstance(login_response, nio.LoginResponse):
            print(f"❌ Login failed: {login_response}")
            return False
        print("✓ Logged in successfully")

        # Give agents time to sync
        print("⏳ Waiting 5s for agents to sync...")
        await asyncio.sleep(5)

        print(f"\n📍 Testing in room: {TEST_ROOM_ID}")

        # Test 1: Multiple agents mentioned (explicit team)
        print("\n🧪 Test 1: Multiple agents mentioned form a team")
        print("   Agents: @calculator, @general")
        print("   Message: @mindroom_calculator @mindroom_general what is 10 + 20 and explain why")

        event_id = await send_message_with_mentions(
            client,
            TEST_ROOM_ID,
            "@mindroom_calculator @mindroom_general what is 10 + 20 and explain why",
        )
        print(f"   ✓ Sent (event_id: {event_id})")
        print("   ⏳ Waiting 8s for team response...")
        await asyncio.sleep(8)

        # Test 2: Multiple agents in thread (implicit team)
        print("\n🧪 Test 2: Multiple agents in thread collaborate")
        print("   Starting a thread with two agents...")

        # Create a thread root
        thread_root = await send_message_with_mentions(
            client,
            TEST_ROOM_ID,
            "@mindroom_code @mindroom_security how should we implement user authentication?",
        )
        print(f"   ✓ Thread started (root: {thread_root})")
        await asyncio.sleep(5)

        # Send follow-up without mentioning anyone
        await client.room_send(
            room_id=TEST_ROOM_ID,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "What about session management?",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": thread_root,
                },
            },
        )
        print("   ✓ Sent follow-up (no mentions) - agents should form team")
        print("   ⏳ Waiting 8s for team response...")
        await asyncio.sleep(8)

        # Test 3: Complex query triggers team formation
        print("\n🧪 Test 3: Complex query triggers team formation")
        print("   Message: Create a financial report with data analysis and visualizations")

        event_id = await send_message(
            client, TEST_ROOM_ID, "Create a financial report with data analysis and visualizations"
        )
        print(f"   ✓ Sent (event_id: {event_id})")
        print("   ⏳ Waiting 8s for router to form team...")
        await asyncio.sleep(8)

        # Get recent messages
        print("\n📊 Recent messages:")
        messages = await get_recent_messages(client, TEST_ROOM_ID, 20)

        # Display last messages
        for msg in messages[-10:]:
            print(f"  {msg['sender']}: {msg['body'][:100]}...")

        print("\n✅ Team collaboration test completed!")
        return True


async def main():
    """Run the team collaboration test."""
    print("=" * 60)
    print("MINDROOM TEAM COLLABORATION E2E TEST")
    print("=" * 60)

    # Kill any existing mindroom processes
    print("\n🧹 Cleaning up old processes...")
    subprocess.run(["pkill", "-f", "mindroom run"], capture_output=True)
    await asyncio.sleep(2)

    # Start mindroom
    print("🚀 Starting Mindroom...")
    temp_dir = tempfile.mkdtemp(prefix="mindroom_teams_test_")
    bot_task = asyncio.create_task(_run(log_level="INFO", storage_path=Path(temp_dir)))

    # Wait for startup
    print("⏳ Waiting 15s for bot to start and sync...")
    await asyncio.sleep(15)

    try:
        success = await test_team_collaboration()

        if success:
            print("\n✅ All tests passed!")
            return_code = 0
        else:
            print("\n❌ Tests failed!")
            return_code = 1

    except KeyboardInterrupt:
        print("\n\n🛑 Test interrupted by user")
        return_code = 1
    except Exception as e:
        import traceback

        print(f"\n❌ Test error: {e}")
        print("\nFull traceback:")
        traceback.print_exc()
        return_code = 1
    finally:
        # Clean up
        print("\n🧹 Cleaning up...")
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task

        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

    sys.exit(return_code)


if __name__ == "__main__":
    asyncio.run(main())
