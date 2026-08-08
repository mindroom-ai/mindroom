"""Tests for the disposable Postgres pytest harness."""

import subprocess

import pytest
from pytest_mock import MockerFixture

from tests import conftest


def test_postgres_port_url_uses_explicit_loopback_binding(mocker: MockerFixture) -> None:
    """Docker's reported host must not override the explicit IPv4 binding."""
    mocker.patch.object(
        conftest.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="[::1]:54321\n",
            stderr="",
        ),
    )

    database_url = conftest._wait_for_postgres_container_port("docker", "postgres-test")

    assert database_url == "postgresql://cache:test@127.0.0.1:54321/mindroom"


def test_postgres_server_keeps_its_data_directory_off_disk() -> None:
    """The image's declared volume must be covered, or every run orphans one."""
    command = conftest._postgres_run_command("docker", "postgres-test", "run-id")

    tmpfs = command[command.index("--tmpfs") + 1]
    assert tmpfs.startswith(f"{conftest._POSTGRES_JOURNAL_DATA_DIR}:")
    assert "-v" not in command


def test_removing_the_postgres_server_takes_its_storage_with_it(mocker: MockerFixture) -> None:
    """A bare ``rm -f`` leaves the container's anonymous volumes behind."""
    run = mocker.patch.object(
        conftest.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    conftest._remove_postgres_container("docker", "postgres-test")

    assert run.call_args.args[0] == ["docker", "rm", "-f", "-v", "postgres-test"]


def test_remove_postgres_container_accepts_missing_container(mocker: MockerFixture) -> None:
    """Docker auto-removal racing controller cleanup is expected."""
    mocker.patch.object(
        conftest.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error response from daemon: No such container: postgres-test",
        ),
    )

    conftest._remove_postgres_container("docker", "postgres-test")


def test_remove_postgres_container_rejects_cleanup_failure(mocker: MockerFixture) -> None:
    """Unexpected cleanup failures must not silently leak containers."""
    mocker.patch.object(
        conftest.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="permission denied",
        ),
    )

    with pytest.raises(RuntimeError, match="permission denied"):
        conftest._remove_postgres_container("docker", "postgres-test")
