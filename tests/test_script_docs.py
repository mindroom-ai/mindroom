"""Contract checks for the background-script operator guide."""

from pathlib import Path


def test_background_script_docs_cover_security_and_lifecycle() -> None:
    """The guide must name every control and safety boundary users rely on."""
    text = Path("docs/tools/background-scripts.md").read_text(encoding="utf-8")

    for required in (
        "start_script",
        "get_script",
        "cancel_script",
        "list_scripts",
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
    assert "MINDROOM_SCRIPT_GATEWAY_URL" in environment_reference
    assert "MINDROOM_SCRIPT_RETENTION_SECONDS" in environment_reference

    generated_reference = Path("skills/mindroom-docs/references/page__tools__background-scripts__index.md")
    assert generated_reference.read_text(encoding="utf-8").startswith("# Background Python Scripts")
