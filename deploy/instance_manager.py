#!/usr/bin/env python3
"""Ultra-simple Mindroom instance manager.

No over-engineering, just the basics.
"""
# ruff: noqa: ANN001, ANN201, PTH123, S602, D205

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REGISTRY_FILE = "instances.json"
ENV_TEMPLATE = ".env.template"


def load_registry():
    """Load the instance registry."""
    if not Path(REGISTRY_FILE).exists():
        # Use local data directory for testing, can be changed to /mnt/data in production
        data_base = os.environ.get("MINDROOM_DATA_BASE", "./instance_data")
        return {
            "instances": {},
            "allocated_ports": {"backend": [], "frontend": []},
            "defaults": {"backend_port_start": 8765, "frontend_port_start": 3003, "data_dir_base": data_base},
        }
    with open(REGISTRY_FILE) as f:
        return json.load(f)


def save_registry(registry):
    """Save the instance registry."""
    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def find_next_ports(registry):
    """Find the next available ports."""
    defaults = registry["defaults"]
    allocated = registry["allocated_ports"]

    backend_port = defaults["backend_port_start"]
    while backend_port in allocated["backend"]:
        backend_port += 1

    frontend_port = defaults["frontend_port_start"]
    while frontend_port in allocated["frontend"]:
        frontend_port += 1

    return backend_port, frontend_port


def create_instance(name, domain=None):
    """Create a new instance."""
    registry = load_registry()

    if name in registry["instances"]:
        print(f"Instance '{name}' already exists!")
        return False

    backend_port, frontend_port = find_next_ports(registry)
    data_dir = f"{registry['defaults']['data_dir_base']}/{name}"

    instance = {
        "name": name,
        "backend_port": backend_port,
        "frontend_port": frontend_port,
        "data_dir": data_dir,
        "domain": domain or f"{name}.localhost",
        "status": "created",
    }

    # Create instance env file
    env_file = f".env.{name}"
    if Path(ENV_TEMPLATE).exists():
        shutil.copy(ENV_TEMPLATE, env_file)
    else:
        # Create basic env file if template doesn't exist
        Path(env_file).touch()

    # Append instance-specific vars
    with open(env_file, "a") as f:
        f.write("\n# Instance configuration\n")
        f.write(f"INSTANCE_NAME={name}\n")
        f.write(f"BACKEND_PORT={backend_port}\n")
        f.write(f"FRONTEND_PORT={frontend_port}\n")
        f.write(f"DATA_DIR={data_dir}\n")
        f.write(f"INSTANCE_DOMAIN={instance['domain']}\n")

    # Update registry
    registry["instances"][name] = instance
    registry["allocated_ports"]["backend"].append(backend_port)
    registry["allocated_ports"]["frontend"].append(frontend_port)
    save_registry(registry)

    print(f"Created instance '{name}':")
    print(f"  Backend port: {backend_port}")
    print(f"  Frontend port: {frontend_port}")
    print(f"  Data dir: {data_dir}")
    print(f"  Domain: {instance['domain']}")
    print(f"  Env file: .env.{name}")
    return True


def start_instance(name):
    """Start an instance."""
    registry = load_registry()
    if name not in registry["instances"]:
        print(f"Instance '{name}' not found!")
        return False

    env_file = f".env.{name}"
    if not Path(env_file).exists():
        print(f"Environment file {env_file} not found!")
        return False

    # Create data directories
    instance = registry["instances"][name]
    for subdir in ["config", "tmp", "logs"]:
        Path(f"{instance['data_dir']}/{subdir}").mkdir(parents=True, exist_ok=True)

    # Start with docker compose (modern syntax)
    cmd = f"docker compose --env-file {env_file} -p {name} up -d"
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, check=False, shell=True)

    if result.returncode == 0:
        registry["instances"][name]["status"] = "running"
        save_registry(registry)
        print(f"Instance '{name}' started successfully!")
        return True
    print(f"Failed to start instance '{name}'")
    return False


def stop_instance(name):
    """Stop an instance."""
    registry = load_registry()
    if name not in registry["instances"]:
        print(f"Instance '{name}' not found!")
        return False

    cmd = f"docker compose -p {name} down"
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, check=False, shell=True)

    if result.returncode == 0:
        registry["instances"][name]["status"] = "stopped"
        save_registry(registry)
        print(f"Instance '{name}' stopped!")
        return True
    print(f"Failed to stop instance '{name}'")
    return False


def list_instances():
    """List all instances."""
    registry = load_registry()
    instances = registry["instances"]

    if not instances:
        print("No instances configured.")
        return

    print("\nInstances:")
    print("-" * 60)
    for name, config in instances.items():
        print(f"{name}:")
        print(f"  Status: {config['status']}")
        print(f"  Ports: {config['backend_port']} (backend), {config['frontend_port']} (frontend)")
        print(f"  Domain: {config['domain']}")
        print(f"  Data: {config['data_dir']}")
        print()


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python instance_manager.py create <name> [domain]")
        print("  python instance_manager.py start <name>")
        print("  python instance_manager.py stop <name>")
        print("  python instance_manager.py list")
        sys.exit(1)

    command = sys.argv[1]

    if command == "create":
        if len(sys.argv) < 3:
            print("Usage: python instance_manager.py create <name> [domain]")
            sys.exit(1)
        name = sys.argv[2]
        domain = sys.argv[3] if len(sys.argv) > 3 else None
        create_instance(name, domain)

    elif command == "start":
        if len(sys.argv) < 3:
            print("Usage: python instance_manager.py start <name>")
            sys.exit(1)
        start_instance(sys.argv[2])

    elif command == "stop":
        if len(sys.argv) < 3:
            print("Usage: python instance_manager.py stop <name>")
            sys.exit(1)
        stop_instance(sys.argv[2])

    elif command == "list":
        list_instances()

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
