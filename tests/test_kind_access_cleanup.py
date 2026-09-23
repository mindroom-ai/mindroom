"""Exercise access-helper process ownership without cluster or HTTP calls."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest

type _AccessHelper = tuple[Callable[..., subprocess.Popen[str]], subprocess.Popen[bytes], Path]

_SCRIPT = Path(__file__).resolve().parents[1] / "cluster/k8s/kind/test-access.sh"


def _write_command(shell: str, directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!{shell}\nset -eu\n" + body)
    command.chmod(0o755)


def _wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("Controlled child did not reach the expected state")


@pytest.fixture
def access_helper(tmp_path: Path) -> Iterator[_AccessHelper]:
    """Stub external commands; all signals target children created by this fixture."""
    shell = shutil.which("bash")
    real_sleep = shutil.which("sleep")
    assert shell is not None
    assert real_sleep is not None
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("grep", "wc", "awk", "cat", "mktemp", "rm"):
        executable = shutil.which(name)
        assert executable is not None
        (binaries / name).symlink_to(executable)
    events = tmp_path / "events"
    events.write_text("")
    _write_command(
        shell,
        binaries,
        "kubectl",
        """
case " $* " in
    *" port-forward "*)
        echo "forward $*" >> "$EVENTS"
        if [[ "$*" == *"${BUSY_SERVICE:-never-match}"* ]]; then
            echo "Unable to listen: bind: address already in use" >&2
            exit 1
        fi
        echo "started $$" >> "$EVENTS"
        trap 'echo "stopped $$" >> "$EVENTS"; exit 0' TERM INT
        if [[ "$*" == *"${DELAY_SERVICE:-never-match}"* ]]; then
            echo "pending $$" >> "$EVENTS"
            read -r startup < "$CONTROL_FIFO"
            if [[ "$startup" == fail ]]; then
                bind-port "$CONTROL_PORT"
                exit 1
            fi
        fi
        mapping="${!#}"
        echo "Forwarding from 127.0.0.1:${mapping%:*} -> ${mapping#*:}"
        while :; do "$TEST_SLEEP" 0.02; done
        ;;
    *" get pods "*)
        if [[ "${FAIL_PODS:-0}" == 1 ]]; then exit 7; fi
        ;;
    *) exit 9 ;;
esac
""",
    )
    _write_command(shell, binaries, "curl", 'echo "curl $*" >> "$EVENTS"\necho "200 ok"\n')
    _write_command(shell, binaries, "sleep", 'echo "wait $*" >> "$EVENTS"\n"$TEST_SLEEP" 0.02\n')
    bind_port = binaries / "bind-port"
    bind_port.write_text(
        f"#!{sys.executable}\n"
        "import socket, sys\n"
        "with socket.socket() as listener:\n"
        "    try:\n"
        "        listener.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "    except OSError as error:\n"
        "        print(f'Unable to listen: {error}', file=sys.stderr)\n"
        "        raise SystemExit(1) from error\n",
    )
    bind_port.chmod(0o755)
    # Model the dangerous command using only the unrelated fixture child.
    # Never invoke the system pkill or inspect the host process list.
    _write_command(shell, binaries, "pkill", 'echo "global-kill" >> "$EVENTS"\nkill -TERM "$UNRELATED_PID"\n')
    environment = {
        **os.environ,
        "PATH": str(binaries),
        "EVENTS": str(events),
        "TEST_SLEEP": real_sleep,
        "TMPDIR": str(tmp_path),
    }
    unrelated = subprocess.Popen(
        [str(binaries / "kubectl"), "port-forward", "svc/unrelated", "9000:80"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for(lambda: f"started {unrelated.pid}" in events.read_text())
    environment["UNRELATED_PID"] = str(unrelated.pid)
    # Sandbox the original helper's fixed diagnostic log paths.
    script = tmp_path / "test-access.sh"
    processes = []

    def launch(*, ingress_port: int = 8080, **overrides: str) -> subprocess.Popen[str]:
        script.write_text(
            _SCRIPT.read_text().replace("/tmp/", f"{tmp_path}/").replace("8080", str(ingress_port)),  # noqa: S108
        )
        process = subprocess.Popen(
            [shell, str(script)],
            env={**environment, **overrides},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        processes.append(process)
        return process

    yield launch, unrelated, events

    # Only signal process groups created above, even when assertions fail.
    for process in [*processes, unrelated]:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def test_access_preserves_unrelated_forwarding(access_helper: _AccessHelper) -> None:
    """Global name-based cleanup must not terminate another forwarding session."""
    launch, unrelated, _ = access_helper
    process = launch()
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0, output
    assert unrelated.poll() is None, "Access check terminated an unrelated forwarding session"


@pytest.mark.parametrize("termination", ["normal", "command_failure", "signal"])
def test_access_reaps_owned_forwarding_on_exit(access_helper: _AccessHelper, termination: str) -> None:
    """Normal completion, errexit, and SIGTERM must stop all owned forwarders."""
    launch, unrelated, events = access_helper
    process = launch(FAIL_PODS="1" if termination == "command_failure" else "0")
    if termination == "signal":
        _wait_for(lambda: len([line for line in events.read_text().splitlines() if line.startswith("started ")]) > 1)
        process.send_signal(signal.SIGTERM)
    output, _ = process.communicate(timeout=10)
    if termination == "normal":
        assert process.returncode == 0, output
    else:
        assert process.returncode != 0, output
    lines = events.read_text().splitlines()
    owned = {line.split()[1] for line in lines if line.startswith("started ")} - {str(unrelated.pid)}
    assert owned, "Fixture must start at least one owned forwarder"
    stopped = {line.split()[1] for line in lines if line.startswith("stopped ")}
    assert owned <= stopped, f"Forwarders escaped cleanup: {owned - stopped}"


@pytest.mark.parametrize("service", ["ingress-nginx-controller", "platform-frontend", "platform-backend"])
def test_access_reports_busy_port_without_probing_it(access_helper: _AccessHelper, service: str) -> None:
    """A failed forward must not let HTTP on somebody else's port appear healthy."""
    launch, unrelated, events = access_helper
    process = launch(BUSY_SERVICE=service)
    output, _ = process.communicate(timeout=10)
    assert process.returncode != 0, output
    assert "address already in use" in output.lower()
    port = {"ingress-nginx-controller": "8080", "platform-frontend": "3000", "platform-backend": "8000"}[service]
    assert not any(f":{port}" in line for line in events.read_text().splitlines() if line.startswith("curl "))
    assert unrelated.poll() is None


def test_access_binds_and_probes_same_loopback_address(access_helper: _AccessHelper) -> None:
    """Partial dual-stack binding must not send probes to somebody else's listener."""
    launch, _, events = access_helper
    process = launch()
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0, output
    lines = events.read_text().splitlines()
    forwards = [line for line in lines if line.startswith("forward ") and "svc/unrelated" not in line]
    assert len(forwards) == 3
    assert all("--address 127.0.0.1" in line for line in forwards)
    probes = [line for line in lines if line.startswith("curl ")]
    urls = [argument for line in probes for argument in line.split() if argument.startswith("http://")]
    assert urls == [
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8080/api/health",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000/health",
    ]
    assert all("Host: platform.local" in line for line in probes[:2])


@pytest.mark.parametrize(
    ("service", "port"),
    [("ingress-nginx-controller", 8080), ("platform-frontend", 3000), ("platform-backend", 8000)],
)
def test_access_waits_for_owned_listener(
    access_helper: _AccessHelper,
    tmp_path: Path,
    service: str,
    port: int,
) -> None:
    """A live child that has not announced its bind must not permit HTTP probes."""
    launch, unrelated, events = access_helper
    control = tmp_path / "startup"
    os.mkfifo(control)
    process = launch(DELAY_SERVICE=service, CONTROL_FIFO=str(control))
    _wait_for(lambda: "pending " in events.read_text())
    pending_lines = events.read_text().splitlines()
    waits_before = sum(line.startswith("wait ") for line in pending_lines)

    def probed_or_waited() -> bool:
        lines = events.read_text().splitlines()
        return any(line.startswith("curl ") and f":{port}" in line for line in lines) or (
            sum(line.startswith("wait ") for line in lines) > waits_before
        )

    _wait_for(probed_or_waited)
    assert not any(line.startswith("curl ") and f":{port}" in line for line in events.read_text().splitlines())
    control.write_text("ready\n")
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0, output
    assert any(line.startswith("curl ") and f":{port}" in line for line in events.read_text().splitlines())
    assert unrelated.poll() is None


def test_access_delayed_bind_failure_preserves_unrelated_listener(
    access_helper: _AccessHelper,
    tmp_path: Path,
) -> None:
    """A slow child must fail its actual bind before somebody else's port is probed."""
    launch, unrelated, events = access_helper
    control = tmp_path / "startup"
    os.mkfifo(control)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = launch(
            ingress_port=port,
            DELAY_SERVICE="ingress-nginx-controller",
            CONTROL_FIFO=str(control),
            CONTROL_PORT=str(port),
        )
        _wait_for(lambda: "pending " in events.read_text())
        waits_before = sum(line.startswith("wait ") for line in events.read_text().splitlines())

        def probed_or_waited() -> bool:
            lines = events.read_text().splitlines()
            return any(line.startswith("curl ") for line in lines) or (
                sum(line.startswith("wait ") for line in lines) > waits_before
            )

        _wait_for(probed_or_waited)
        assert not any(line.startswith("curl ") for line in events.read_text().splitlines())
        control.write_text("fail\n")
        output, _ = process.communicate(timeout=10)
        assert process.returncode != 0, output
        assert "address already in use" in output.lower()
        assert not any(line.startswith("curl ") for line in events.read_text().splitlines())
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            connection, _ = listener.accept()
            connection.close()
    assert unrelated.poll() is None
