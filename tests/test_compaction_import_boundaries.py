"""History imports must not mutate process-global Agno behavior."""

import subprocess


def test_history_import_does_not_install_sdk_patches() -> None:
    """Patch installation belongs to owned runtime construction, not imports."""
    code = """
from agno.agent import _messages as am, _session as ase, _storage as ast
from agno.team import _messages as tm, _session as tse

def surfaces():
    return (am.get_run_messages, am.aget_run_messages,
            tm._get_run_messages, tm._aget_run_messages,
            ast.aread_session, ase.asave_session, ase.asave_run,
            tse.aget_session, tse.asave_session, tse.asave_run)

before = surfaces()
import mindroom.history.runtime
assert surfaces() == before, "history import installed global SDK patches"
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
