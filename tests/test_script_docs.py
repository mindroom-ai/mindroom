"""Contract checks for the background-script operator guide."""

from pathlib import Path


def test_background_script_docs_cover_security_and_lifecycle() -> None:
    """The guide must name every control and safety boundary users rely on."""
    text = Path("docs/tools/background-scripts.md").read_text(encoding="utf-8")

    for required in (
        "run_script",
        "status_script",
        "cancel_script",
        "allowed_tools",
        "MindRoomTools.call",
        "ignore_mentions=False",
        "interrupted",
        "indeterminate",
        "MINDROOM_SCRIPT_GATEWAY_URL",
        "MINDROOM_SCRIPT_RETENTION_SECONDS",
        "local execution",
    ):
        assert required in text

    environment_reference = Path("docs/configuration/index.md").read_text(encoding="utf-8")
    assert "MINDROOM_SCRIPT_RETENTION_SECONDS" in environment_reference
