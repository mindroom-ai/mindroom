#!/usr/bin/env python3
"""Manual test for thread invitations."""

import asyncio

import nio

HOMESERVER = "http://localhost:8008"


async def main():
    """Test thread invitations manually."""
    # Create test user
    client = nio.AsyncClient(HOMESERVER, "@manual_test:localhost")

    # Register test user
    response = await client.register(
        username="manual_test", password="test_password_123", device_name="manual_test_device"
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

    # Start a thread
    print("\n🧵 Starting a thread...")
    thread_start_response = await client.room_send(
        room_id=lobby_room_id,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "Starting a test thread for invitations"},
    )

    if isinstance(thread_start_response, nio.RoomSendResponse):
        thread_id = thread_start_response.event_id
        print(f"✅ Thread started: {thread_id}")
    else:
        print(f"❌ Failed to start thread: {thread_start_response}")
        await client.close()
        return

    # Wait a moment
    await asyncio.sleep(2)

    # Send invite command in thread
    print("\n📨 Sending /invite command in thread...")
    invite_response = await client.room_send(
        room_id=lobby_room_id,
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": "/invite calculator",
            "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
        },
    )

    if isinstance(invite_response, nio.RoomSendResponse):
        print("✅ Sent invite command")
    else:
        print(f"❌ Failed to send invite: {invite_response}")

    # Wait for processing
    print("\n⏳ Waiting 5s for bot to process...")
    await asyncio.sleep(5)

    # Mention calculator in thread
    print("\n💬 Mentioning calculator in thread...")
    mention_response = await client.room_send(
        room_id=lobby_room_id,
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": "@mindroom_calculator:localhost what is 2 + 2?",
            "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            "m.relates_to": {"rel_type": "m.thread", "event_id": thread_id},
        },
    )

    if isinstance(mention_response, nio.RoomSendResponse):
        print("✅ Sent mention to calculator")
    else:
        print(f"❌ Failed to send mention: {mention_response}")

    # Wait and check for responses
    print("\n⏳ Waiting 10s for responses...")
    await asyncio.sleep(10)

    # Fetch recent messages
    print("\n📥 Fetching thread messages...")
    messages_response = await client.room_messages(lobby_room_id, limit=50)

    if isinstance(messages_response, nio.RoomMessagesResponse):
        thread_messages = []
        for event in messages_response.chunk:
            if isinstance(event, nio.RoomMessageText):
                relates_to = event.source.get("content", {}).get("m.relates_to", {})
                if relates_to.get("event_id") == thread_id and relates_to.get("rel_type") == "m.thread":
                    sender = event.sender.split(":")[0].replace("@", "")
                    thread_messages.append(f"{sender}: {event.body}")

        print(f"\n📋 Found {len(thread_messages)} thread messages:")
        for msg in thread_messages:
            print(f"   - {msg}")

    # Clean up
    await client.close()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(main())
