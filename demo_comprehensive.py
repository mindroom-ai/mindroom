#!/usr/bin/env python3
"""Comprehensive demo script with room creation and response monitoring.

This script can:
- Create a new demo room
- Invite agents to the room
- Send various test messages
- Monitor responses in real-time
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import nio
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

# Add src to path so we can import mindroom modules
sys.path.insert(0, str(Path(__file__).parent))

from src.mindroom.matrix_config import MatrixConfig

# Load environment variables
load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")

console = Console()


class DemoOrchestrator:
    """Orchestrates the demo with room management and monitoring."""

    def __init__(self):
        self.config = MatrixConfig.load()
        self.client = None
        self.room_id = None
        self.messages_sent = []
        self.responses_received = []
        self.monitoring = True

    async def setup_client(self) -> None:
        """Setup the demo client."""
        if "user" not in self.config.accounts:
            console.print("❌ No mindroom_user found. Please run: mindroom user create", style="bold red")
            raise Exception("No user credentials")

        user_account = self.config.get_account("user")
        self.client = nio.AsyncClient(MATRIX_HOMESERVER, f"@{user_account.username}:localhost")

        response = await self.client.login(user_account.password)
        if isinstance(response, nio.LoginResponse):
            console.print(f"✅ Logged in as {user_account.username}", style="bold green")
        else:
            raise Exception(f"Login failed: {response}")

    async def create_demo_room(self) -> str:
        """Create a new room for the demo."""
        room_name = f"Mindroom Demo - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        response = await self.client.room_create(
            name=room_name,
            topic="Automated demo room for testing mindroom agents",
            preset=nio.RoomPreset.private_chat,
        )

        if isinstance(response, nio.RoomCreateResponse):
            console.print(f"✅ Created room: {room_name}", style="bold green")
            console.print(f"   Room ID: {response.room_id}", style="dim")
            return response.room_id
        else:
            raise Exception(f"Failed to create room: {response}")

    async def invite_agents(self, room_id: str) -> list[str]:
        """Invite all agents to the room."""
        invited_agents = []

        for account_key, account in self.config.accounts.items():
            if account_key.startswith("agent_"):
                user_id = f"@{account.username}:localhost"
                response = await self.client.room_invite(room_id, user_id)

                if isinstance(response, nio.RoomInviteResponse):
                    agent_name = account_key.replace("agent_", "")
                    console.print(f"✅ Invited {agent_name} ({user_id})", style="green")
                    invited_agents.append(agent_name)
                else:
                    console.print(f"❌ Failed to invite {user_id}: {response}", style="red")

        return invited_agents

    async def send_demo_message(self, room_id: str, message: str, message_type: str = "regular", **kwargs) -> str:
        """Send a demo message and track it."""
        event_id = None

        if message_type == "mention":
            content = {
                "msgtype": "m.text",
                "body": f"@{kwargs['agent_name']}: {message}",
                "m.mentions": {"user_ids": [kwargs["user_id"]]},
            }
        elif message_type == "thread":
            content = {
                "msgtype": "m.text",
                "body": message,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": kwargs["thread_id"],
                },
            }
        else:
            content = {
                "msgtype": "m.text",
                "body": message,
            }

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if isinstance(response, nio.RoomSendResponse):
            event_id = response.event_id
            self.messages_sent.append(
                {
                    "time": datetime.now(),
                    "type": message_type,
                    "message": message,
                    "event_id": event_id,
                    "details": kwargs,
                }
            )

        return event_id

    async def monitor_responses(self, room_id: str) -> None:
        """Monitor room for responses."""

        # Set up event callback
        def on_message(room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
            if event.sender != self.client.user_id and event.sender.startswith("@mindroom_"):
                agent_name = event.sender.split(":")[0].replace("@mindroom_", "")
                self.responses_received.append(
                    {
                        "time": datetime.now(),
                        "agent": agent_name,
                        "message": event.body,
                        "event_id": event.event_id,
                    }
                )

        self.client.add_event_callback(on_message, nio.RoomMessageText)

        # Start syncing
        await self.client.sync(timeout=30000, full_state=True)

    def display_status(self) -> Table:
        """Create a status table."""
        table = Table(title="Demo Status", show_header=True, header_style="bold magenta")
        table.add_column("Time", style="dim", width=12)
        table.add_column("Type", style="cyan")
        table.add_column("Message", style="white")
        table.add_column("Response", style="green")

        # Combine sent messages and responses
        all_events = []

        for msg in self.messages_sent:
            all_events.append(
                {
                    "time": msg["time"],
                    "type": f"SENT ({msg['type']})",
                    "message": msg["message"][:50] + "..." if len(msg["message"]) > 50 else msg["message"],
                    "response": "",
                }
            )

        for resp in self.responses_received:
            all_events.append(
                {
                    "time": resp["time"],
                    "type": f"RESPONSE ({resp['agent']})",
                    "message": "",
                    "response": resp["message"][:50] + "..." if len(resp["message"]) > 50 else resp["message"],
                }
            )

        # Sort by time
        all_events.sort(key=lambda x: x["time"])

        # Add to table (last 10 events)
        for event in all_events[-10:]:
            table.add_row(event["time"].strftime("%H:%M:%S"), event["type"], event["message"], event["response"])

        return table

    async def run_demo_sequence(self, room_id: str, agents: list[str]) -> None:
        """Run the full demo sequence."""
        console.print("\n" + "=" * 60, style="bold blue")
        console.print("🎬 Starting Demo Sequence", style="bold white")
        console.print("=" * 60 + "\n", style="bold blue")

        # Start monitoring task
        monitor_task = asyncio.create_task(self.continuous_monitoring(room_id))

        try:
            # Demo 1: Test calculator agent
            if "calculator" in agents:
                console.print("1️⃣  Testing calculator agent with direct mention...", style="bold yellow")
                await self.send_demo_message(
                    room_id,
                    "What is 42 * 10?",
                    "mention",
                    agent_name="calculator",
                    user_id="@mindroom_calculator:localhost",
                )
                await asyncio.sleep(3)

            # Demo 2: Test general agent
            if "general" in agents:
                console.print("\n2️⃣  Testing general agent with direct mention...", style="bold yellow")
                await self.send_demo_message(
                    room_id,
                    "What's the weather like today?",
                    "mention",
                    agent_name="general",
                    user_id="@mindroom_general:localhost",
                )
                await asyncio.sleep(3)

            # Demo 3: Create a thread
            console.print("\n3️⃣  Creating a thread for discussion...", style="bold yellow")
            thread_id = await self.send_demo_message(
                room_id, "Let's have a discussion about programming languages!", "regular"
            )
            await asyncio.sleep(2)

            # Demo 4: Reply in thread
            if thread_id:
                console.print("\n4️⃣  Sending message in thread (only one agent should respond)...", style="bold yellow")
                await self.send_demo_message(
                    room_id, "What are the advantages of Python?", "thread", thread_id=thread_id
                )
                await asyncio.sleep(3)

            # Demo 5: Multiple agent mentions
            if len(agents) >= 2:
                console.print("\n5️⃣  Testing multiple agent interaction...", style="bold yellow")
                for agent in agents[:2]:
                    await self.send_demo_message(
                        room_id,
                        f"Hello {agent}, can you introduce yourself?",
                        "mention",
                        agent_name=agent,
                        user_id=f"@mindroom_{agent}:localhost",
                    )
                    await asyncio.sleep(2)

            # Demo 6: Message with no trigger
            console.print("\n6️⃣  Sending regular message (should be ignored)...", style="bold yellow")
            await self.send_demo_message(room_id, "This is just a regular message with no mentions.", "regular")
            await asyncio.sleep(2)

            console.print("\n✅ Demo sequence completed!", style="bold green")
            console.print("\nPress Ctrl+C to stop monitoring and exit.", style="dim")

            # Keep monitoring
            await monitor_task

        except KeyboardInterrupt:
            console.print("\n\n👋 Stopping demo...", style="bold yellow")
            self.monitoring = False
            monitor_task.cancel()

    async def continuous_monitoring(self, room_id: str) -> None:
        """Continuously monitor and display status."""
        with Live(self.display_status(), refresh_per_second=1) as live:
            while self.monitoring:
                await self.client.sync(timeout=1000, full_state=False)
                live.update(self.display_status())
                await asyncio.sleep(0.5)

    async def cleanup(self) -> None:
        """Cleanup resources."""
        if self.client:
            await self.client.close()


async def main():
    """Main entry point."""
    console.print(
        Panel.fit(
            "[bold]Mindroom Comprehensive Demo[/bold]\n\n"
            "This script will:\n"
            "• Create a new demo room\n"
            "• Invite all configured agents\n"
            "• Send various test messages\n"
            "• Monitor responses in real-time",
            title="🤖 Welcome",
            border_style="blue",
        )
    )

    orchestrator = DemoOrchestrator()

    try:
        # Setup
        await orchestrator.setup_client()

        # Ask user for room preference
        console.print("\nChoose an option:", style="bold")
        console.print("1. Create a new demo room")
        console.print("2. Use existing room")
        choice = console.input("\nYour choice (1 or 2): ")

        if choice == "1":
            room_id = await orchestrator.create_demo_room()
            agents = await orchestrator.invite_agents(room_id)
            await asyncio.sleep(2)  # Give agents time to join
        else:
            room_id = console.input("Enter room ID: ").strip()
            # Assume all agents are in the room
            agents = [key.replace("agent_", "") for key in orchestrator.config.accounts if key.startswith("agent_")]

        # Run demo
        await orchestrator.run_demo_sequence(room_id, agents)

    except Exception as e:
        console.print(f"\n❌ Error: {e}", style="bold red")
    finally:
        await orchestrator.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n👋 Goodbye!", style="bold green")
