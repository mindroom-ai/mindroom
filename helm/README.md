# MindRoom Helm Chart

Minimal Kubernetes deployment for MindRoom using Helm.

## Features

- **MindRoom Application**: Single container running both frontend (port 3003) and backend (port 8765)
- **Matrix Server**: Synapse with SQLite for simple, lightweight chat infrastructure
- **Persistent Storage**: Data volumes for both MindRoom and Synapse
- **Simple Configuration**: Minimal setup with basic agents and settings

## Quick Start

```bash
cd mindroom/

# Install with your API keys
helm install demo . \
  --set customer=demo \
  --set domain=demo.mindroom.chat \
  --set openai_key=$OPENAI_API_KEY

# Or use the setup script
./setup.sh demo demo.mindroom.chat
```

## Structure

```
helm/
├── mindroom/           # Main Helm chart
│   ├── Chart.yaml      # Chart metadata
│   ├── values.yaml     # Default values
│   ├── templates/      # Kubernetes manifests
│   │   └── all.yaml    # All resources (~280 lines)
│   ├── setup.sh        # Quick install script
│   └── README.md       # Detailed documentation
└── shell.nix           # Nix environment for testing
```

## Why Minimal?

This chart prioritizes simplicity over features:
- SQLite instead of PostgreSQL (suitable for <100 users)
- No Redis caching layer
- No Authelia (uses Matrix's built-in auth)
- Single file template for easy understanding
- Basic configuration with essential agents only

Perfect for development, testing, or small deployments.
