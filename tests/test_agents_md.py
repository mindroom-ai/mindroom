"""Keep AGENTS.md within the project-instruction budget that coding agents load."""

from pathlib import Path

AGENTS_MD = Path(__file__).resolve().parents[1] / "AGENTS.md"

# Codex loads at most `project_doc_max_bytes` (32 KiB by default) of project instructions and silently drops the rest.
CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024


def test_agents_md_fits_codex_project_doc_budget() -> None:
    """Every AGENTS.md rule reaches Codex agents only if the whole file fits the budget."""
    size = len(AGENTS_MD.read_bytes())
    assert size <= CODEX_PROJECT_DOC_MAX_BYTES, (
        f"AGENTS.md is {size} bytes, over Codex's {CODEX_PROJECT_DOC_MAX_BYTES}-byte limit; "
        "move reference material into the files AGENTS.md links to."
    )
