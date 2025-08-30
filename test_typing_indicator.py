"""Test script to demonstrate typing indicators for MindRoom agents."""

import asyncio
import os

from dotenv import load_dotenv

from mindroom.matrix.client import create_matrix_client
from mindroom.matrix.typing import typing_indicator

# Load environment variables
load_dotenv()


async def test_typing_indicator() -> None:
    """Test typing indicator functionality."""
    # Get Matrix credentials from environment
    homeserver = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
    username = os.getenv("MATRIX_USERNAME", "@mindroom_assistant:localhost")
    password = os.getenv("MATRIX_PASSWORD", "")
    test_room = os.getenv("TEST_ROOM_ID", "!test:localhost")

    if not password:
        print("❌ Please set MATRIX_PASSWORD environment variable")
        return

    print(f"🔑 Connecting to {homeserver} as {username}")

    # Create client and login
    client = create_matrix_client(homeserver)
    response = await client.login(password, device_name="typing-test")

    if hasattr(response, "access_token"):
        print("✅ Login successful")
    else:
        print(f"❌ Login failed: {response}")
        return

    try:
        # Join the test room if not already in it
        print(f"📍 Testing in room: {test_room}")

        # Demonstrate typing indicator
        print("\n🎯 Showing typing indicator for 5 seconds...")
        async with typing_indicator(client, test_room, timeout_ms=30000):
            print("   ⌨️ Typing indicator is now visible in the room")
            await asyncio.sleep(5)
            print("   ⏸️ Simulating AI response generation...")

        print("✅ Typing indicator stopped\n")

        # Demonstrate multiple typing sessions
        print("🔄 Demonstrating multiple typing sessions...")
        for i in range(3):
            print(f"\n   Session {i + 1}:")
            async with typing_indicator(client, test_room):
                print("   ⌨️ Agent is typing... (2 seconds)")
                await asyncio.sleep(2)
            print(f"   💬 Message {i + 1} sent")
            await asyncio.sleep(1)

        print("\n✨ Test completed successfully!")
        print("\nTo see the typing indicators:")
        print("1. Open your Matrix client (Element, etc.)")
        print(f"2. Join room {test_room}")
        print("3. Watch for the '... is typing' indicator")
        print("\nYou can also use Matty CLI:")
        print(f"matty messages '{test_room}' --limit 10")

    finally:
        await client.close()


if __name__ == "__main__":
    print("🚀 MindRoom Typing Indicator Test\n")
    asyncio.run(test_typing_indicator())
