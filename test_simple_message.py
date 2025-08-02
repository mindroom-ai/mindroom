#!/usr/bin/env python3
"""Simple test to send a message to lobby."""

import asyncio

import nio

HOMESERVER = "http://localhost:8008"


async def main():
    """Send a simple message to test agents."""
    # Create test user
    client = nio.AsyncClient(HOMESERVER, "@test_user_simple:localhost")

    # Register or login
    response = await client.register(
        username="test_user_simple", password="test_password_123", device_name="simple_test"
    )

    if isinstance(response, nio.RegisterResponse):
        print("✅ Registered test user")
        client.access_token = response.access_token
        client.device_id = response.device_id
    elif hasattr(response, "status_code") and response.status_code == "M_USER_IN_USE":
        print("ℹ️  User exists, logging in...")
        response = await client.login("test_password_123")
        if not isinstance(response, nio.LoginResponse):
            print(f"❌ Login failed: {response}")
            await client.close()
            return

    # Join lobby room
    print("\n🚪 Joining lobby room...")
    response = await client.join("#lobby:localhost")
    if isinstance(response, nio.JoinResponse):
        lobby_room_id = response.room_id
        print(f"✅ Joined lobby: {lobby_room_id}")
    else:
        print(f"❌ Failed to join lobby: {response}")
        await client.close()
        return

    # Send a simple message
    print("\n💬 Sending message to lobby...")
    response = await client.room_send(
        room_id=lobby_room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "Hello everyone! Can anyone help me?"},
    )

    if isinstance(response, nio.RoomSendResponse):
        print("✅ Sent message")
    else:
        print(f"❌ Failed to send: {response}")

    # Wait and check for responses
    print("\n⏳ Waiting 10s for responses...")
    await asyncio.sleep(10)

    # Fetch recent messages
    print("\n📥 Fetching recent messages...")
    messages_response = await client.room_messages(lobby_room_id, limit=20)

    if isinstance(messages_response, nio.RoomMessagesResponse):
        messages = []
        for event in messages_response.chunk:
            if isinstance(event, nio.RoomMessageText):
                sender = event.sender.split(":")[0].replace("@", "")
                messages.append(f"{sender}: {event.body[:100]}")

        print(f"\n📋 Last {len(messages)} messages:")
        for msg in messages[-10:]:
            print(f"   - {msg}")

    # Clean up
    await client.close()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(main())
