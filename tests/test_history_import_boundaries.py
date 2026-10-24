"""History imports change no process-global Agno behavior, and the summary call never loads Agno."""

from __future__ import annotations

import subprocess
import sys

_SURFACES = """
from agno.agent import _messages as am, _session as ase, _storage as ast
from agno.models.base import Model
from agno.team import _messages as tm, _session as tse


def surfaces():
    return (
        am.get_run_messages, am.aget_run_messages, tm._get_run_messages, tm._aget_run_messages,
        ast.aread_session, ase.asave_session, ase.asave_run, tse.aget_session, tse.asave_session, tse.asave_run,
        Model.aresponse, Model.aresponse_stream, Model.response, Model.response_stream,
    )
"""


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=120)


def test_history_imports_install_no_agno_patches() -> None:
    """Importing the history runtime and the compaction engine leaves every Agno loop and storage function as shipped."""
    result = _run(
        _SURFACES
        + """
before = surfaces()
import mindroom.history.compaction
import mindroom.history.runtime
assert surfaces() == before, "a history import replaced an Agno function"
""",
    )
    assert result.returncode == 0, result.stderr


def test_the_summary_call_loads_no_agno_module() -> None:
    """The compaction summary call and its retry policy run on the owned provider layer alone."""
    result = _run(
        """
import sys
import mindroom.history.summary_call
loaded = sorted(name for name in sys.modules if name == "agno" or name.startswith("agno."))
assert not loaded, loaded
""",
    )
    assert result.returncode == 0, result.stderr
