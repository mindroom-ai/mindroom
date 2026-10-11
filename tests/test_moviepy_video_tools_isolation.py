"""Renderer fixtures restore real factories regardless of import order."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ISOLATION_PROBE = """
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "src"))
module_names = ("agno.tools.moviepy_video", "mindroom.custom_tools.agno_compat_moviepy")
assert not any(name in sys.modules for name in ("moviepy", *module_names))
if sys.argv[1] == "warm":
    for name in module_names:
        importlib.import_module(name)

status = pytest.main(["tests/test_moviepy_video_tools.py", "-n", "0", "--no-cov", "-q"])
assert status == 0, status
moviepy = importlib.import_module("moviepy")
for name in module_names:
    module = importlib.import_module(name)
    for factory in ("TextClip", "ColorClip", "CompositeVideoClip", "VideoFileClip"):
        assert getattr(module, factory) is getattr(moviepy, factory), (name, factory)

adapter = importlib.import_module(module_names[1])
clip = adapter.ColorClip((2, 2), color=(17, 34, 51), duration=1)
try:
    frame = clip.get_frame(0)
    assert frame.shape == (2, 2, 3)
    assert frame[0, 0].tolist() == [17, 34, 51]
finally:
    clip.close()
"""


@pytest.mark.parametrize("import_order", ["cold", "warm"])
def test_renderer_fixture_restores_real_factories(import_order: str) -> None:
    """Later tests get real MoviePy even when fake rendering ran first."""
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE, import_order],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
