"""Internal module entrypoint for knowledge refresh subprocesses."""

from __future__ import annotations

from mindroom.knowledge.refresh_worker import main

if __name__ == "__main__":
    raise SystemExit(main())
