#!/usr/bin/env python
"""Simple test to check if memory is working."""

import subprocess
from pathlib import Path

# Check if mindroom is running
result = subprocess.run(["pgrep", "-f", "mindroom run"], capture_output=True)
if not result.stdout:
    print("Mindroom is not running! Please start it with: mindroom run")
    exit(1)

print("Mindroom is running. Checking memory system...")

# Check if ChromaDB directory exists
chroma_path = Path("tmp/chroma")
if chroma_path.exists():
    print(f"✓ Memory storage directory exists: {chroma_path}")
    # List contents
    for item in chroma_path.rglob("*"):
        if item.is_file():
            print(f"  - {item.relative_to('tmp')}")
else:
    print(f"✗ Memory storage directory not found: {chroma_path}")
    print("\nTo test memory:")
    print("1. Connect to Matrix with a client (Element, etc)")
    print("2. Send a message mentioning an agent, e.g., '@mindroom_calculator:localhost what is 2+2?'")
    print("3. Wait for the response")
    print("4. Run this script again to check if memory was created")

# Check agent response tracking DBs
print("\nAgent databases:")
for db_file in Path("tmp").glob("*.db"):
    size = db_file.stat().st_size
    print(f"  - {db_file.name}: {size:,} bytes")

# Check if Ollama is running
result = subprocess.run(["pgrep", "-f", "ollama"], capture_output=True)
if result.stdout:
    print("\n✓ Ollama is running")
else:
    print("\n✗ Ollama is not running - memory embeddings won't work!")
    print("  Start it with: ollama serve")
