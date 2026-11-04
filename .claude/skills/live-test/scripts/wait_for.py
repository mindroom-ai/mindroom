r"""Wait for a real condition instead of sleeping: a URL answering 200, or a regex in a log after a byte offset.

Note the log size before acting, so an older line cannot satisfy the wait:

    offset=$(stat -c %s "$run_root/backend.log")
    # ... send the message ...
    wait_for.py --log "$run_root/backend.log" --offset "$offset" --regex 'Sent hook message' --fail-regex '\[error'
    wait_for.py --url http://localhost:9876/api/health

Prints the matching line and exits 0, exits 3 when ``--fail-regex`` matches first, and exits 2 on timeout.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
from pathlib import Path


def _line_at(text: str, match: re.Match[str]) -> str:
    """Return the whole line a match is on."""
    end = text.find("\n", match.end())
    return text[text.rfind("\n", 0, match.start()) + 1 : None if end < 0 else end]


def _url_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - the operator chooses the URL
            return response.status == 200
    except OSError:
        return False


def main() -> None:
    """Poll until the condition holds, the fail pattern appears, or the timeout passes."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--offset", type=int, default=0, help="byte offset in the log to search from")
    parser.add_argument("--regex")
    parser.add_argument("--fail-regex", help="stop with exit code 3 when this matches first")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if not args.url and not (args.log and args.regex):
        parser.error("give --url, or --log with --regex")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if args.url and _url_ready(args.url):
            print(f"ok {args.url}")
            return
        if args.log and args.log.exists():
            # Each poll searches everything after the offset again, so the earlier of the two matches decides.
            text = args.log.read_bytes()[args.offset :].decode(errors="replace")
            matched = re.search(args.regex, text) if args.regex else None
            failed = re.search(args.fail_regex, text) if args.fail_regex else None
            if failed and (matched is None or failed.start() <= matched.start()):
                print(f"FAIL {_line_at(text, failed)[:800]}")
                sys.exit(3)
            if matched:
                print(_line_at(text, matched)[:800])
                return
        time.sleep(args.interval)
    print("TIMEOUT", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
