"""Tests for skill API endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mindroom import skills as skills_module


def _write_skill(root: Path, name: str = "test-skill", description: str = "Test skill") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {description}",
                "---",
                "",
                f"# {name}",
            ],
        ),
        encoding="utf-8",
    )
    return skill_file


def test_list_skills(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """List skills with metadata."""
    _write_skill(tmp_path)
    monkeypatch.setattr(skills_module, "get_default_skill_roots", lambda: [tmp_path])
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: tmp_path)

    response = test_client.get("/api/skills")
    assert response.status_code == 200

    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "test-skill"
    assert data[0]["description"] == "Test skill"
    assert data[0]["can_edit"] is True


def test_get_skill(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch skill content."""
    _write_skill(tmp_path)
    monkeypatch.setattr(skills_module, "get_default_skill_roots", lambda: [tmp_path])
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: tmp_path)

    response = test_client.get("/api/skills/test-skill")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "test-skill"
    assert payload["content"].startswith("---")


def test_update_skill(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update skill content when editable."""
    skill_file = _write_skill(tmp_path)
    monkeypatch.setattr(skills_module, "get_default_skill_roots", lambda: [tmp_path])
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: tmp_path)

    updated_content = "---\nname: test-skill\ndescription: Test skill\n---\n\n# Updated"
    response = test_client.put("/api/skills/test-skill", json={"content": updated_content})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert skill_file.read_text(encoding="utf-8") == updated_content


def test_update_skill_forbidden(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block updates for read-only skills."""
    _write_skill(tmp_path)
    monkeypatch.setattr(skills_module, "get_default_skill_roots", lambda: [tmp_path])
    monkeypatch.setattr(skills_module, "get_user_skills_dir", lambda: tmp_path / "other")

    response = test_client.put("/api/skills/test-skill", json={"content": "---"})
    assert response.status_code == 403


def test_get_skill_not_found(test_client: TestClient) -> None:
    """Return 404 when skill does not exist."""
    response = test_client.get("/api/skills/unknown")
    assert response.status_code == 404
