"""The compaction summary call never loads Agno."""

from __future__ import annotations

import subprocess
import sys

_CODE = """
import sys
import mindroom.history.summary_call
loaded = sorted(name for name in sys.modules if name == "agno" or name.startswith("agno."))
assert not loaded, loaded
"""


def test_the_summary_call_loads_no_agno_module() -> None:
    """The compaction summary call and its retry policy run on the owned provider layer alone."""
    result = subprocess.run([sys.executable, "-c", _CODE], check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
