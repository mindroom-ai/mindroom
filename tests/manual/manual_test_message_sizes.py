#!/usr/bin/env python
"""Test different message sizes to see attachment behavior.

This is a manual test script, not part of the automated test suite.
Run it manually with: python tests/manual/test_message_sizes.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from mindroom.matrix.client import get_joined_rooms, get_room_name, matrix_client, send_message


async def test_message_sizes() -> None:  # noqa: PLR0915
    """Test messages of different sizes."""
    # Get credentials from environment
    homeserver = os.getenv("MATRIX_HOMESERVER", "https://m-test-3.mindroom.chat")
    username = os.getenv("MATRIX_USERNAME", "mindroom_user")
    password = os.getenv("MATRIX_PASSWORD", "user_secure_password")

    # Construct full user ID
    server_name = homeserver.replace("https://", "").replace("http://", "").split(":")[0]
    user_id = f"@{username}:{server_name}"

    print(f"🔌 Connecting to {homeserver} as {user_id}")

    async with matrix_client(homeserver, user_id) as client:
        # Login
        print("🔐 Logging in...")
        response = await client.login(password, device_name="test_sizes")
        if not hasattr(response, "access_token"):
            print(f"❌ Login failed: {response}")
            return

        print("✅ Logged in successfully")

        # Get joined rooms
        rooms = await get_joined_rooms(client)
        if not rooms:
            print("❌ No rooms joined")
            return

        # Let user choose room or use first one
        if len(rooms) > 1:
            print("\n📍 Available rooms:")
            for i, rid in enumerate(rooms[:10], 1):
                room_name = await get_room_name(client, rid)
                print(f"  {i}. {room_name} ({rid})")
            print("\n  Press Enter to use first room, or type a number:")
            choice = input("  > ").strip()
            if choice and choice.isdigit():
                idx = int(choice) - 1
                room_id = rooms[idx] if 0 <= idx < len(rooms) else rooms[0]
            else:
                room_id = rooms[0]
        else:
            room_id = rooms[0]

        room_name = await get_room_name(client, room_id)
        print(f"📍 Using room: {room_name}")

        # Test 1: Short message (1KB)
        print("\n" + "=" * 60)
        print("📝 Test 1: SHORT MESSAGE (1KB)")
        print("=" * 60)
        short_text = """🟢 SHORT MESSAGE TEST (1KB)

This is a short message that should appear normally without any attachment.
It's well under the limit and should just display as regular text.

Features tested:
- Normal message display
- No attachment needed
- Standard Matrix message

""" + ("This is some padding text to reach approximately 1KB. " * 10)

        content = {"body": short_text, "msgtype": "m.text"}
        print(f"📊 Size: {len(short_text):,} bytes")
        event_id = await send_message(client, room_id, content)
        if event_id:
            print(f"✅ Sent: {event_id}")
            print("👁️ Should appear as: Normal message, NO attachment")

        # Wait a bit between messages
        await asyncio.sleep(2)

        # Test 2: Just under limit (52KB - should pass through)
        print("\n" + "=" * 60)
        print("📝 Test 2: JUST UNDER LIMIT (52KB)")
        print("=" * 60)
        medium_text = """🟡 JUST UNDER LIMIT TEST (52KB)

This message is just under the threshold where attachments are needed.
It should still appear as a normal message without any attachment.

This represents a typical long AI response that fits within Matrix limits.

""" + ("This is content that makes the message approximately 52KB in size. " * 750)

        content = {"body": medium_text, "msgtype": "m.text"}
        print(f"📊 Size: {len(medium_text):,} bytes")
        event_id = await send_message(client, room_id, content)
        if event_id:
            print(f"✅ Sent: {event_id}")
            print("👁️ Should appear as: Normal message, NO attachment")

        # Wait a bit between messages
        await asyncio.sleep(2)

        # Test 3: Slightly over limit (65KB - needs attachment)
        print("\n" + "=" * 60)
        print("📝 Test 3: SLIGHTLY OVER LIMIT (65KB)")
        print("=" * 60)
        over_text = """🟠 SLIGHTLY OVER LIMIT TEST (65KB)

This message exceeds the Matrix event size limit and requires attachment handling.
You should see this as a preview with a file attachment.

The attachment contains the full message content.

""" + ("This is content that makes the message approximately 65KB in size, triggering the attachment mechanism. " * 900)

        content = {"body": over_text, "msgtype": "m.text"}
        print(f"📊 Size: {len(over_text):,} bytes")
        event_id = await send_message(client, room_id, content)
        if event_id:
            print(f"✅ Sent: {event_id}")
            print("👁️ Should appear as: Preview text + 📎 message.txt attachment")

        # Wait a bit between messages
        await asyncio.sleep(2)

        # Test 4: Much larger (500KB - definitely needs attachment)
        print("\n" + "=" * 60)
        print("📝 Test 4: MUCH LARGER (500KB)")
        print("=" * 60)
        large_text = """🔴 VERY LARGE MESSAGE TEST (500KB)

This is a very large message that definitely requires attachment handling.
This simulates a comprehensive AI response with extensive detail.

You should see a preview with [Message continues in attached file] and a file attachment.

Key points about large messages:
1. Preview shows first ~50KB of content
2. Full content available in attachment
3. Attachment named 'message.txt'
4. Custom metadata for future client support

""" + (
            "This represents a very detailed response that might come from an AI assistant providing comprehensive information. "
            * 5000
        )

        content = {"body": large_text, "msgtype": "m.text"}
        print(f"📊 Size: {len(large_text):,} bytes")
        event_id = await send_message(client, room_id, content)
        if event_id:
            print(f"✅ Sent: {event_id}")
            print("👁️ Should appear as: Preview text + 📎 message.txt attachment")

        print("\n" + "=" * 60)
        print("✨ ALL TESTS COMPLETED!")
        print("=" * 60)
        print("\n📱 Check your Matrix client to see:")
        print("  1️⃣ SHORT (1KB): Normal message")
        print("  2️⃣ UNDER LIMIT (52KB): Normal message")
        print("  3️⃣ SLIGHTLY OVER (65KB): Preview + attachment")
        print("  4️⃣ MUCH LARGER (500KB): Preview + attachment")
        print("\n📎 Attachments appear at the TOP of the message bubble")
        print("📝 Preview text appears BELOW the attachment")


if __name__ == "__main__":
    # Load environment variables
    from dotenv import load_dotenv

    load_dotenv()

    # Set SSL verification from env
    if os.getenv("MATRIX_SSL_VERIFY", "true").lower() == "false":
        os.environ["MATRIX_SSL_VERIFY"] = "false"

    print("🧪 Testing Different Message Sizes")
    print("=" * 60)

    try:
        asyncio.run(test_message_sizes())
    except KeyboardInterrupt:
        print("\n⚠️  Test interrupted")
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
