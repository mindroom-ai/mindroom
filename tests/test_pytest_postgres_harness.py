"""Tests for the disposable Postgres pytest harness."""

import os
import shutil
import signal
import subprocess
import uuid

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
    mounts = [command[index + 1] for index, argument in enumerate(command) if argument == "-v"]
    assert all(conftest._POSTGRES_JOURNAL_DATA_DIR not in mount for mount in mounts)


def test_postgres_server_watches_the_run_that_owns_it() -> None:
    """A killed run reaches no teardown, so its server must end itself instead."""
    command = conftest._postgres_run_command("docker", "postgres-test", "run-id")
    owner_pipe_dir = conftest._owner_pipe_dir("run-id")

    assert f"{owner_pipe_dir}:{conftest._POSTGRES_OWNER_MOUNT_DIR}:ro" in command
    assert command[command.index("--entrypoint") + 1] == "bash"
    assert command[-1] == conftest._POSTGRES_JOURNAL_ENTRYPOINT_SCRIPT


def test_owner_death_escalates_past_a_bootstrap_that_ignores_sigint() -> None:
    """SIGINT alone leaks a container when the owner dies early, and that is measured.

    Once the postmaster is serving, SIGINT is its fast shutdown and the
    container goes within a second. During bootstrap the signal lands on
    `docker-entrypoint.sh` running `initdb`, which survives it: an owner killed
    in that window left the container up and still serving ninety seconds
    later. With the escalation it goes after the grace period instead.
    """
    script = conftest._POSTGRES_JOURNAL_ENTRYPOINT_SCRIPT
    interrupt, kill = script.index('kill -INT "$server"'), script.index('kill -KILL "$server"')

    assert interrupt < kill
    assert f"sleep {conftest._POSTGRES_OWNER_SHUTDOWN_GRACE_SECONDS}" in script[interrupt:kill]


def test_postgres_server_admits_every_worker_n_auto_starts() -> None:
    """The default cap turns worker count alone into a wall of red.

    `postgres:16` allows a hundred connections and `PostgresBackend.open` takes
    `_POOL_SIZE` readers plus one writer for every store, so twenty concurrent
    stores exhaust the default and `-n auto` asks for one per worker before a
    single test opens a second. Measured over the same 348 tests with only `-n`
    differing: thirty-two workers gave 137 errors, all `sorry, too many clients
    already`, and sixteen gave none.

    The demand is derived from the pool size and this host rather than written
    down, so a wider reader pool or a bigger machine fails here instead of
    resurfacing as unexplained red somewhere else. It follows that the check is
    only meaningful where the bug is: a four-core runner needs twenty
    connections and would be fine on the stock hundred.
    """
    # Deferred because it imports psycopg, which is an optional extra, and the
    # rest of this file must still be collectable without it.
    from mindroom.event_journal.postgres_backend import _POOL_SIZE  # noqa: PLC0415

    connections_per_store = _POOL_SIZE + 1
    n_auto_demand = (os.cpu_count() or 1) * connections_per_store

    assert n_auto_demand <= conftest._POSTGRES_JOURNAL_MAX_CONNECTIONS
    assert (
        f"-c max_connections={conftest._POSTGRES_JOURNAL_MAX_CONNECTIONS}"
        in conftest._POSTGRES_JOURNAL_ENTRYPOINT_SCRIPT
    )


def test_postgres_server_actually_starts_with_the_raised_cap(postgres_journal_url: str) -> None:
    """Only the running server can say the flag reached the postmaster.

    The entrypoint hands the argument to `docker-entrypoint.sh`, which passes
    its arguments on to `postgres`. An argument dropped anywhere along that
    path leaves a server quietly running on the default hundred, which is
    indistinguishable from the fix working until a full `-n auto` run.
    """
    import psycopg  # noqa: PLC0415

    with psycopg.connect(postgres_journal_url) as db:
        cap = db.execute("SHOW max_connections").fetchone()

    assert cap is not None
    assert int(cap[0]) == conftest._POSTGRES_JOURNAL_MAX_CONNECTIONS


def test_owner_pipe_reports_the_owner_s_death_and_nothing_else() -> None:
    """The whole fix rests on this pipe meaning exactly one thing.

    Read the way the container's watcher reads it: a living owner must never
    look like a dead one, because that mistake destroys another agent's suite
    mid-run, and a dead owner must be reported without anything having to ask.
    """
    run_id = f"harness-{uuid.uuid4().hex}"
    ready_read, ready_write = os.pipe()
    owner = os.fork()
    if owner == 0:  # pragma: no cover - the child is killed, never measured
        try:
            os.close(ready_read)
            conftest._hold_owner_pipe(run_id)
            os.write(ready_write, b"1")
            while True:
                signal.pause()
        finally:
            # A forked child must never unwind back into pytest, which would
            # report and tear down the rest of the session a second time.
            os._exit(1)

    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"1"
        pipe = conftest._owner_pipe_dir(run_id) / conftest._POSTGRES_OWNER_PIPE_NAME
        # O_NONBLOCK so that opening cannot block on a writer, exactly as the
        # watcher opens it -- an owner that died first must report, not hang.
        watcher = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(BlockingIOError):
                os.read(watcher, 4096)

            os.kill(owner, signal.SIGKILL)
            os.waitpid(owner, 0)

            os.set_blocking(watcher, True)
            assert os.read(watcher, 4096) == b""
        finally:
            os.close(watcher)
    finally:
        os.close(ready_read)
        shutil.rmtree(conftest._owner_pipe_dir(run_id), ignore_errors=True)


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
