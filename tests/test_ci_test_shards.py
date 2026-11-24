"""CI shards split the test files so every file runs in exactly one shard."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import pytest_ignore_collect

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_every_test_file_lands_in_exactly_one_of_three_shards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collection hook keeps each real test file in one shard and skips it in the other two."""
    config = SimpleNamespace(rootpath=REPO_ROOT)
    test_files = sorted((REPO_ROOT / "tests").rglob("test_*.py"))
    assert len(test_files) > 100
    kept_by: dict[Path, list[int]] = {path: [] for path in test_files}
    for index in (1, 2, 3):
        monkeypatch.setenv("MINDROOM_TEST_SHARD", f"{index}/3")
        for path in test_files:
            if pytest_ignore_collect(path, config) is None:
                kept_by[path].append(index)
    assert {path: shards for path, shards in kept_by.items() if len(shards) != 1} == {}
    assert {shards[0] for shards in kept_by.values()} == {1, 2, 3}


def test_unset_shard_collects_everything_and_leaves_non_test_files_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local runs without the variable, directories, and conftests are never skipped."""
    config = SimpleNamespace(rootpath=REPO_ROOT)
    monkeypatch.delenv("MINDROOM_TEST_SHARD", raising=False)
    assert pytest_ignore_collect(Path(__file__), config) is None
    monkeypatch.setenv("MINDROOM_TEST_SHARD", "1/3")
    assert pytest_ignore_collect(REPO_ROOT / "tests", config) is None
    assert pytest_ignore_collect(REPO_ROOT / "tests" / "conftest.py", config) is None


@pytest.mark.parametrize("value", ["3", "0/3", "4/3", "a/b", "1/0"])
def test_malformed_shard_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A typo in the CI matrix fails loudly instead of silently running the wrong files."""
    monkeypatch.setenv("MINDROOM_TEST_SHARD", value)
    with pytest.raises(pytest.UsageError, match="MINDROOM_TEST_SHARD"):
        pytest_ignore_collect(Path(__file__), SimpleNamespace(rootpath=REPO_ROOT))
