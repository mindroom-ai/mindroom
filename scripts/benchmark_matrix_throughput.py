#!/usr/bin/env python
"""Benchmark script to test Matrix server message throughput.

This script tests the raw performance of the Matrix protocol/server
by sending messages at various rates and measuring throughput.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

# Suppress all debug logs for cleaner benchmark output
os.environ["MINDROOM_LOG_LEVEL"] = "WARNING"  # Set before importing mindroom
logging.getLogger("mindroom").setLevel(logging.WARNING)
logging.getLogger("mindroom.matrix").setLevel(logging.WARNING)
logging.getLogger("mindroom.matrix.client").setLevel(logging.WARNING)

from mindroom.matrix import MATRIX_HOMESERVER  # noqa: E402
from mindroom.matrix.client import send_message  # noqa: E402
from mindroom.matrix.users import AgentMatrixUser, login_agent_user  # noqa: E402

# Suppress structlog output after import (mindroom uses structlog)
try:
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )
except ImportError:
    pass

if TYPE_CHECKING:
    import nio


@dataclass
class BenchmarkMetrics:
    """Metrics collected during benchmark."""

    messages_sent: int = 0
    messages_failed: int = 0
    start_time: float = 0
    end_time: float = 0

    @property
    def duration(self) -> float:
        """Calculate the duration of the benchmark."""
        return self.end_time - self.start_time

    @property
    def throughput(self) -> float:
        """Calculate messages per second throughput."""
        if self.duration == 0:
            return 0
        return self.messages_sent / self.duration

    @property
    def success_rate(self) -> float:
        """Calculate the percentage of successfully sent messages."""
        total = self.messages_sent + self.messages_failed
        if total == 0:
            return 0
        return self.messages_sent / total * 100


class MatrixBenchmark:
    """Benchmark Matrix server message throughput."""

    def __init__(self, homeserver: str | None = None) -> None:
        self.homeserver = homeserver or MATRIX_HOMESERVER
        self.client: nio.AsyncClient | None = None
        self.room_id: str | None = None
        self.metrics = BenchmarkMetrics()
        self.agent_user: AgentMatrixUser | None = None

    async def setup(self) -> None:
        """Load credentials and setup client."""
        # Load credentials from matrix_state.yaml
        with Path("matrix_state.yaml").open() as f:  # noqa: ASYNC230
            data = yaml.safe_load(f)

        # Use the main user account
        user_data = data["accounts"]["agent_user"]
        username = user_data["username"]
        password = user_data["password"]

        # Use lobby room for testing
        self.room_id = data["rooms"]["lobby"]["room_id"]

        # Extract domain from homeserver
        domain = self.homeserver.split("://")[1] if "://" in self.homeserver else self.homeserver

        # Create AgentMatrixUser object
        self.agent_user = AgentMatrixUser(
            agent_name="benchmark",
            user_id=f"@{username}:{domain}",
            display_name="Benchmark Bot",
            password=password,
        )

    async def login(self) -> None:
        """Login to Matrix server."""
        print(f"🔑 Logging in as {self.agent_user.user_id} to {self.homeserver}...")

        # Use the login utility from mindroom
        self.client = await login_agent_user(self.homeserver, self.agent_user)
        print(f"✅ Logged in successfully as {self.agent_user.user_id}")

    async def send_message(self, content: str, batch_id: int, msg_id: int) -> bool:
        """Send a single message and return success status."""
        try:
            message_content = {"msgtype": "m.text", "body": f"[Benchmark B{batch_id:03d}M{msg_id:04d}] {content}"}

            # Use the send_message utility from mindroom
            event_id = await send_message(self.client, self.room_id, message_content)

            if event_id:
                return True
            print("❌ Failed to send message")
            return False  # noqa: TRY300

        except Exception as e:
            print(f"❌ Error sending message: {e}")
            return False

    async def run_burst_test(self, num_messages: int, concurrency: int = 1) -> BenchmarkMetrics:
        """Send messages in a burst with TRUE concurrency - all at once."""
        print(f"\n🚀 Burst Test: {num_messages} messages with TRUE concurrency={concurrency}")

        self.metrics = BenchmarkMetrics()

        # Create ALL tasks at once - no batching!
        tasks = []
        for i in range(num_messages):
            batch_id = i // max(1, concurrency)
            msg_id = i
            task = self.send_message(f"Burst test message {i + 1}/{num_messages}", batch_id, msg_id)
            tasks.append(task)

        # Execute ALL tasks concurrently
        print(f"    Sending {len(tasks)} messages simultaneously...")
        self.metrics.start_time = time.time()  # Start timer RIGHT before sending
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count results
        for result in results:
            if isinstance(result, bool) and result:
                self.metrics.messages_sent += 1
            else:
                self.metrics.messages_failed += 1

        self.metrics.end_time = time.time()

        print(f"✅ Sent: {self.metrics.messages_sent}/{num_messages}")
        print(f"❌ Failed: {self.metrics.messages_failed}")
        print(f"⏱️  Duration: {self.metrics.duration:.2f}s")
        print(f"📊 Throughput: {self.metrics.throughput:.2f} msg/s")
        print(f"📈 Success Rate: {self.metrics.success_rate:.1f}%")

        return self.metrics

    async def run_sustained_test(self, duration_seconds: int, messages_per_second: float) -> BenchmarkMetrics:
        """Send messages at a sustained rate for a specified duration."""
        print(f"\n⏳ Sustained Test: {messages_per_second} msg/s for {duration_seconds}s")

        self.metrics = BenchmarkMetrics()
        self.metrics.start_time = time.time()

        interval = 1.0 / messages_per_second
        end_time = self.metrics.start_time + duration_seconds
        msg_count = 0

        while time.time() < end_time:
            loop_start = time.time()

            # Send message
            success = await self.send_message(f"Sustained test at {messages_per_second} msg/s", 0, msg_count)

            if success:
                self.metrics.messages_sent += 1
            else:
                self.metrics.messages_failed += 1

            msg_count += 1

            # Sleep to maintain rate
            elapsed = time.time() - loop_start
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

        self.metrics.end_time = time.time()

        print(f"✅ Sent: {self.metrics.messages_sent}")
        print(f"❌ Failed: {self.metrics.messages_failed}")
        print(f"⏱️  Duration: {self.metrics.duration:.2f}s")
        print(f"📊 Actual Throughput: {self.metrics.throughput:.2f} msg/s")
        print(f"📈 Success Rate: {self.metrics.success_rate:.1f}%")

        return self.metrics

    async def run_ramp_test(self, max_rate: float, step_duration: int = 5) -> list[tuple[int, BenchmarkMetrics]]:
        """Gradually increase message rate to find maximum throughput."""
        print(f"\n📈 Ramp Test: 0 to {max_rate} msg/s")

        rates = [1, 2, 5, 10, 20, 30, 40, 50, 75, 100, 200]
        rates = [r for r in rates if r <= max_rate]

        results: list[tuple[int, BenchmarkMetrics]] = []

        for rate in rates:
            print(f"\n🔄 Testing {rate} msg/s...")
            metrics = await self.run_sustained_test(step_duration, rate)
            results.append((rate, metrics))

            # Stop if success rate drops below 90%
            if metrics.success_rate < 90:
                print(f"⚠️  Success rate dropped below 90% at {rate} msg/s")
                break

            # Brief pause between tests
            await asyncio.sleep(1)

        # Print summary
        print("\n📊 Ramp Test Summary:")
        print("Rate (msg/s) | Actual (msg/s) | Success Rate")
        print("-" * 45)
        for target_rate, metrics in results:
            print(f"{target_rate:11.1f} | {metrics.throughput:14.2f} | {metrics.success_rate:11.1f}%")

        return results

    async def cleanup(self) -> None:
        """Close client connection."""
        if self.client:
            await self.client.close()


async def main() -> None:
    """Run the benchmark suite."""
    benchmark = MatrixBenchmark()

    try:
        # Setup and login
        await benchmark.setup()
        await benchmark.login()

        print("\n" + "=" * 60)
        print("🎯 Matrix Server Throughput Benchmark")
        print("=" * 60)

        # Test 1: Burst test with different concurrency levels
        print("\n### BURST TESTS ###")
        for concurrency in [1, 5, 10, 20]:
            await benchmark.run_burst_test(100, concurrency=concurrency)
            await asyncio.sleep(2)  # Brief pause between tests

        # Test 2: Sustained rate test
        print("\n### SUSTAINED RATE TESTS ###")
        for rate in [5, 10, 20, 50]:
            await benchmark.run_sustained_test(duration_seconds=10, messages_per_second=rate)
            await asyncio.sleep(2)

        # Test 3: Ramp test to find maximum sustainable rate
        print("\n### RAMP TEST ###")
        await benchmark.run_ramp_test(max_rate=500, step_duration=5)

        print("\n" + "=" * 60)
        print("✅ Benchmark Complete!")
        print("=" * 60)

    finally:
        await benchmark.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
