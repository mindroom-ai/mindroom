#!/usr/bin/env python
"""Test script to send a message to the bot and verify it responds."""

import asyncio
import sys
from pathlib import Path

import nio
import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from mindroom.matrix import MATRIX_HOMESERVER

# Load config to get room ID
with open("config.yaml") as f:
    config = yaml.safe_load(f)

# Get the first room from config - using the default rooms
default_rooms = config.get("defaults", {}).get("rooms", ["general"])
room_alias = default_rooms[0] if isinstance(default_rooms, list) else list(default_rooms)[0]
room_id_or_alias = f"#{room_alias}:localhost"


async def send_test_message():
    """Send a test message to the bot."""
    # Use the mindroom_user that was created during setup
    import os

    username = os.getenv("MATRIX_USERNAME", "mindroom_user")
    password = os.getenv("MATRIX_PASSWORD", "mindroom_password")

    # Create a test client
    client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{username}:localhost")

    # Login
    response = await client.login(password, device_name="test_script")
    if not isinstance(response, nio.LoginResponse):
        print(f"Failed to login: {response}")
        return False

    print(f"Logged in as {client.user_id}")

    # Join the room
    join_response = await client.join(room_id_or_alias)
    if isinstance(join_response, nio.JoinResponse):
        room_id = join_response.room_id
        print(f"Joined room {room_id}")
    else:
        print(f"Failed to join room: {join_response}")
        await client.close()
        return False

    # Send a test message mentioning an agent
    test_messages = [
        "@mindroom_calculator:localhost what is 42 * 17?",
        "@mindroom_general:localhost what did I just calculate?",
        "@mindroom_calculator:localhost do you remember what I asked before?",
    ]

    for i, message in enumerate(test_messages):
        print(f"\nSending message {i + 1}: {message}")

        send_response = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": message,
                "format": "org.matrix.custom.html",
                "formatted_body": message,
            },
        )

        if isinstance(send_response, nio.RoomSendResponse):
            print(f"Message sent with event ID: {send_response.event_id}")
        else:
            print(f"Failed to send message: {send_response}")

        # Wait a bit between messages
        if i < len(test_messages) - 1:
            print("Waiting 5 seconds before next message...")
            await asyncio.sleep(5)

    # Wait for responses
    print("\nWaiting 10 seconds for bot responses...")
    await asyncio.sleep(10)

    # Fetch recent messages
    messages_response = await client.room_messages(room_id, limit=20)
    if isinstance(messages_response, nio.RoomMessagesResponse):
        print("\nRecent messages in room:")
        for event in reversed(messages_response.chunk):
            if isinstance(event, nio.RoomMessageText):
                sender = event.sender
                body = event.body[:100] + "..." if len(event.body) > 100 else event.body
                print(f"  {sender}: {body}")

    await client.close()
    return True


async def main():
    """Main test function."""

    print("\nMake sure mindroom is running with: mindroom run")
    print("Waiting 5 seconds for bot to be ready...")
    await asyncio.sleep(5)

    print("\nSending test messages...")
    success = await send_test_message()

    if success:
        print("\nTest completed! Check if:")
        print("1. The calculator agent responded to the calculation")
        print("2. The general agent could recall the calculation from memory")
        print("3. The calculator agent remembered the previous question")
        print("\nAlso check if tmp/chroma directory was created for memory storage")
    else:
        print("\nTest failed!")


if __name__ == "__main__":
    asyncio.run(main())
