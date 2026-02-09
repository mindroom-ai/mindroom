# CLI Reference

MindRoom provides a command-line interface for managing agents.

## Basic Usage

```
mindroom [OPTIONS] COMMAND [ARGS]...
```

## Commands

```
 Usage: root [OPTIONS] COMMAND [ARGS]...

 Mindroom: Multi-agent Matrix bot system


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.                │
│ --show-completion             Show completion for the current shell, to copy it or     │
│                               customize the installation.                              │
│ --help                        Show this message and exit.                              │
╰────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────╮
│ version    Show the current version of Mindroom.                                       │
│ validate   Validate the configuration file.                                            │
│ run        Run the mindroom multi-agent system.                                        │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## version

Show the current MindRoom version.

```
 Usage: root version [OPTIONS]

 Show the current version of Mindroom.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                            │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## run

Start MindRoom with your configuration.

```
 Usage: root run [OPTIONS]

 Run the mindroom multi-agent system.

 This command starts the multi-agent bot system which automatically: - Creates all
 necessary user and agent accounts - Creates all rooms defined in config.yaml - Manages
 agent room memberships

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --log-level     -l      TEXT  Set the logging level (DEBUG, INFO, WARNING, ERROR)      │
│                               [default: INFO]                                          │
│ --storage-path  -s      PATH  Base directory for persistent MindRoom data (state,      │
│                               sessions, tracking)                                      │
│                               [default: mindroom_data]                                 │
│ --help                        Show this message and exit.                              │
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
