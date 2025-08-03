#!/usr/bin/env python
"""Test that agents create threads when responding to room messages."""

import asyncio
import sys
from pathlib import Path

import nio
import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mindroom.matrix import MATRIX_HOMESERVER


async def test_thread_creation():
    """Test that agents create threads when replying to room messages."""
    # Load credentials
    with open("matrix_state.yaml") as f:
        users_data = yaml.safe_load(f)

    username = users_data["accounts"]["user"]["username"]
    password = users_data["accounts"]["user"]["password"]
    room_id = users_data["rooms"]["lobby"]["room_id"]

    # Create client
    client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{username}:localhost")

    try:
        # Login
        print(f"🔑 Logging in as {username}...")
        response = await client.login(password, device_name="thread_test")
        if not isinstance(response, nio.LoginResponse):
            raise Exception(f"Failed to login: {response}")
        print("✓ Logged in successfully")
        print(f"Testing in room: {room_id}")

        # Send a message in the room (not in a thread)
        print("\n1. Sending message in room (not in thread)...")
        content = {"msgtype": "m.text", "body": "What is 2 + 2?"}
        response = await client.room_send(room_id, "m.room.message", content)
        if isinstance(response, nio.RoomSendResponse):
            event_id = response.event_id
            print(f"   Sent message with event_id: {event_id}")
        else:
            print(f"   Failed to send: {response}")
            return

        # Wait for response
        print("   Waiting 5s for response...")
        await asyncio.sleep(5)

        # Check messages
        print("\n2. Checking messages...")
        messages_response = await client.room_messages(room_id, limit=10)

        if hasattr(messages_response, "chunk"):
            print(f"\nFound {len(messages_response.chunk)} messages:")
            for event in messages_response.chunk:
                if hasattr(event, "sender") and hasattr(event, "body"):
                    sender = event.sender.split(":")[0].replace("@", "")
                    body = event.body[:100] + "..." if len(event.body) > 100 else event.body

                    # Check if message is in a thread
                    relates_to = getattr(event, "source", {}).get("content", {}).get("m.relates_to", {})
                    if relates_to.get("rel_type") == "m.thread":
                        thread_id = relates_to.get("event_id", "unknown")
                        print(f"🧵 {sender}: {body}")
                        print(f"   Thread root: {thread_id}")
                    else:
                        print(f"💬 {sender}: {body}")

        # Test with mention
        print("\n3. Testing with agent mention...")
        content2 = {
            "msgtype": "m.text",
            "body": "@mindroom_calculator:localhost What is 10 * 5?",
            "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
        }
        response2 = await client.room_send(room_id, "m.room.message", content2)
        if isinstance(response2, nio.RoomSendResponse):
            event_id2 = response2.event_id
            print(f"   Sent mention with event_id: {event_id2}")
        else:
            print(f"   Failed to send: {response2}")
            return

        print("   Waiting 5s for response...")
        await asyncio.sleep(5)

        # Check messages again
        messages_response2 = await client.room_messages(room_id, limit=10)

        if hasattr(messages_response2, "chunk"):
            print(f"\nFound {len(messages_response2.chunk)} messages after mention:")
            for event in messages_response2.chunk:
                if hasattr(event, "sender") and hasattr(event, "body"):
                    sender = event.sender.split(":")[0].replace("@", "")
                    body = event.body[:100] + "..." if len(event.body) > 100 else event.body

                    # Check if message is in a thread
                    relates_to = getattr(event, "source", {}).get("content", {}).get("m.relates_to", {})
                    if relates_to.get("rel_type") == "m.thread":
                        thread_id = relates_to.get("event_id", "unknown")
                        print(f"🧵 {sender}: {body}")
                        print(f"   Thread root: {thread_id}")
                    else:
                        print(f"💬 {sender}: {body}")

    finally:
        await client.close()


async def main():
    """Run the test."""
    print("=" * 60)
    print("THREAD CREATION TEST")
    print("=" * 60)

    # Run mindroom separately first:
    # mindroom run --log-level INFO

    print("\nMake sure mindroom is running!")
    print("Run in another terminal: mindroom run --log-level INFO")
    print("\nStarting test in 5 seconds...")
    await asyncio.sleep(5)

    await test_thread_creation()
    print("\n✅ Test completed!")


if __name__ == "__main__":
    asyncio.run(main())
