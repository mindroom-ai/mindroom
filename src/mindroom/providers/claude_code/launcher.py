"""Scrub inherited variables the Claude Agent SDK cannot drop. This script must stay stdlib-only."""

import os
import signal
import sys

SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
SCRUB_NAMES = {"CLAUDE_BG_AUTH_SNAPSHOT_PATH", "CLAUDE_AUTO_BACKGROUND_TASKS"}
CLI_ENV = "MINDROOM_CLAUDE_CODE_CLI"
KEEP_ENV = "MINDROOM_CLAUDE_CODE_KEEP"


def _main() -> None:
    """Remove untrusted settings and replace this process with Claude Code."""
    real = os.environ.pop(CLI_ENV)
    keep = set(os.environ.pop(KEEP_ENV).split())
    for name in tuple(os.environ):
        if name not in keep and (name.startswith(SCRUB_PREFIXES) or name in SCRUB_NAMES):
            del os.environ[name]
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    os.execv(real, [real, *sys.argv[1:]])  # noqa: S606 - replace this process with the configured CLI


if __name__ == "__main__":
    _main()
