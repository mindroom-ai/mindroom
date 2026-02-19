# CLI Reference

MindRoom provides a command-line interface for managing agents.

## Basic Usage

```
mindroom [OPTIONS] COMMAND [ARGS]...
```

## Commands

```
 Usage: root [OPTIONS] COMMAND [ARGS]...

 AI agents that live in Matrix and work everywhere via bridges.

 Quick start:
 mindroom config init   Create a starter config
 mindroom run           Start the system

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --install-completion            Install completion for the current shell.              │
│ --show-completion               Show completion for the current shell, to copy it or   │
│                                 customize the installation.                            │
│ --help                -h        Show this message and exit.                            │
╰────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────╮
│ version   Show the current version of Mindroom.                                        │
│ run       Run the mindroom multi-agent system.                                         │
│ doctor    Check your environment for common issues.                                    │
│ config    Manage MindRoom configuration files.                                         │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## version

Show the current MindRoom version.

```
 Usage: root version [OPTIONS]

 Show the current version of Mindroom.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## run

Start MindRoom with your configuration.

```
 Usage: root run [OPTIONS]

 Run the mindroom multi-agent system.

 This command starts the multi-agent bot system which automatically:
 - Creates all necessary user and agent accounts
 - Creates all rooms defined in config.yaml
 - Manages agent room memberships
 - Starts the dashboard API server (disable with --no-api)

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --log-level     -l              TEXT     Set the logging level (DEBUG, INFO, WARNING,  │
│                                          ERROR)                                        │
│                                          [env var: LOG_LEVEL]                          │
│                                          [default: INFO]                               │
│ --storage-path  -s              PATH     Base directory for persistent MindRoom data   │
│                                          (state, sessions, tracking)                   │
│                                          [default: mindroom_data]                      │
│ --api               --no-api             Start the dashboard API server alongside the  │
│                                          bot                                           │
│                                          [default: api]                                │
│ --api-port                      INTEGER  Port for the dashboard API server             │
│                                          [default: 8765]                               │
│ --api-host                      TEXT     Host for the dashboard API server             │
│                                          [default: 0.0.0.0]                            │
│ --help          -h                       Show this message and exit.                   │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## Examples

### Basic run

```
mindroom run
```

### Debug logging

```
mindroom run --log-level DEBUG
```

### Custom storage path

```
mindroom run --storage-path /data/mindroom
```

### Show version

```
mindroom version
```
