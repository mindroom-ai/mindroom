"""Kill a live backend while a workflow's one write stands at a barrier, restart it, and count what happened.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.kill_after_write --barrier before-receipt|after-receipt [--slot N]

The script gives the Helper the `workflow` toolkit, restarts the backend with `MINDROOM_WORKFLOW_FAULT_DIR` set, and
asks the Helper to start a run whose only call appends one line to a file.
It waits for the barrier's own `.reached` file, never for the line, so the write is certain to have happened.
Then it kills the backend with SIGKILL, starts it again without the barrier, waits for the run to end, and prints

    lines=<lines in the file> node=<state of the call> reports=<replies in the thread that carry the run's result>

Expected: `lines=1 node=indeterminate reports=0` for before-receipt, `lines=1 node=cached reports=1` for after-receipt.
It exits 0 when the line it printed is the expected one.
Standard library only; Linux only, because it reads the backend's command and environment from /proc.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

_SLOTS = Path("/tmp/mindroom-slots")  # noqa: S108  # where `just live up` keeps a slot's state
_STACKS = Path(os.environ.get("E2E_AGENT_STACK_ROOT") or "/tmp/mindroom-client-agents")  # noqa: S108
_ENDED_RUN_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_EXPECTED = {"before-receipt": "lines=1 node=indeterminate reports=0", "after-receipt": "lines=1 node=cached reports=1"}
# The engine's barrier that stands for each of the two states; the earlier one is released for the later one.
_BARRIERS = {"before-receipt": "before_receipt", "after-receipt": "before_settle"}

_WRITE_ONCE = """\
from mindroom.workflow import shell


def main(args):
    shell("echo a >> {counter}")
    return {{"report": "{report}"}}
"""


class LiveError(Exception):
    """A step of the fault script that did not happen; its text says which."""


def say(message: str) -> None:
    """Print one progress line to stderr, so stdout holds only the result line."""
    print(f"workflow_live: {message}", file=sys.stderr, flush=True)


def until[T](what: str, check: Callable[[], T | None], timeout: float, interval: float = 0.2) -> T:
    """Return *check*'s first result that is not None or False, or raise after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(interval)
    msg = f"{what} did not happen within {timeout:.0f} s"
    raise LiveError(msg)


@dataclass
class Deployment:
    """One `just live up` deployment: its Matrix test account, its slot directory and its backend process."""

    slot: int
    live: dict[str, Any]
    directory: Path

    @classmethod
    def find(cls, slot: int | None) -> Deployment:
        """Return slot *slot*'s ready deployment, or the only ready one."""
        ready = {}
        for path in _SLOTS.glob("*.live.json"):
            state = json.loads(path.read_text())
            if state.get("status") == "ready":
                ready[int(path.name.split(".")[0])] = state
        if slot is None and len(ready) != 1:
            msg = f"found {len(ready)} ready live deployments ({sorted(ready)}); pass --slot N"
            raise LiveError(msg)
        slot = next(iter(ready)) if slot is None else slot
        directory = _STACKS / f"slot-{slot}"
        if slot not in ready:
            # A stack started with `stack.py N up` alone, as the Docker-worker one is: its own state names the same.
            try:
                stack = json.loads((directory / "state.json").read_text())
                tester = stack["tester"]
                ready[slot] = {
                    "homeserver": stack["homeserver"],
                    "lobby": {"roomId": stack["rooms"]["lobby"]},
                    **tester,
                }
            except (OSError, KeyError, ValueError) as exc:
                msg = f"no ready live deployment on slot {slot}; run `just live up --backend <checkout>` first"
                raise LiveError(msg) from exc
        return cls(slot, ready[slot], directory)

    @property
    def state(self) -> dict[str, Any]:
        """Return the stack's own state file, which names the backend's process and its API."""
        return json.loads((self.directory / "state.json").read_text())

    @property
    def workspace(self) -> Path:
        """Return the Helper's workspace, where `start_workflow(path=...)` reads a script."""
        return self.directory / "storage" / "agents" / "helper" / "workspace"

    def backend(self) -> Backend:
        """Return the running backend with the command, directory and environment it was started with."""
        pid = int(self.state["pids"]["backend"])
        proc = Path(f"/proc/{pid}")
        try:
            command = [part.decode() for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
            pairs = [
                pair.decode().split("=", 1) for pair in (proc / "environ").read_bytes().split(b"\0") if b"=" in pair
            ]
            directory = (proc / "cwd").resolve()
        except OSError as exc:
            msg = f"the backend of slot {self.slot} (process {pid}) is not running"
            raise LiveError(msg) from exc
        if str(self.directory) not in " ".join(command):
            msg = f"process {pid} is not the backend of slot {self.slot}"
            raise LiveError(msg)
        return Backend(self, pid, command, directory, dict(pairs))

    def give_workflow_toolkit(self) -> None:
        """Give the Helper `workflow`, and let its shell and scripts run on this machine when no worker is routed."""
        path = self.directory / "config.yaml"
        text = path.read_text()
        if "      - workflow\n" not in text:
            text = text.replace("      - shell\n", "      - shell\n      - workflow\n", 1)
        if "allow_shell_without_worker" not in text and "worker_tools" not in text:
            text = text.replace("defaults:\n", "defaults:\n  allow_shell_without_worker: true\n", 1)
        path.write_text(text)

    def matrix(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET *path* from the homeserver's client API as the test account, or PUT *body* there."""
        request = urllib.request.Request(  # noqa: S310  # the deployment's own homeserver on 127.0.0.1
            f"{self.live['homeserver']}/_matrix/client{path}",
            data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "PUT",
            headers={"Authorization": f"Bearer {self.live['accessToken']}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)

    def ask(self, text: str, dev: Path | None) -> str:
        """Send *text* to the lobby as the test account and return the message's event, the root of its thread.

        With *dev* it goes through `just live ask` there, which types it in Chat and waits for the Helper's reply;
        without, it is sent through the homeserver's client API, for a machine with no browser.
        """
        room = urllib.parse.quote(self.live["lobby"]["roomId"], safe="")
        if dev is None:
            message = {"msgtype": "m.text", "body": text}
            return self.matrix(f"/v3/rooms/{room}/send/m.room.message/{secrets.token_hex(8)}", message)["event_id"]
        command = ["just", "live", "ask", text, "--slot", str(self.slot)]
        if subprocess.run(command, cwd=dev, stdout=sys.stderr, check=False).returncode:
            msg = "`just live ask` failed; `--send api` sends the message without Chat"
            raise LiveError(msg)
        for event in self.matrix(f"/v3/rooms/{room}/messages?dir=b&limit=50")["chunk"]:
            if event["sender"] == self.live["userId"] and event.get("content", {}).get("body") == text:
                return event["event_id"]
        msg = "the lobby does not hold the message `just live ask` sent"
        raise LiveError(msg)

    def replies_with(self, root: str, token: str) -> int:
        """Count the agent's messages in *root*'s thread whose newest text holds *token*."""
        room = urllib.parse.quote(self.live["lobby"]["roomId"], safe="")
        thread = self.matrix(f"/v1/rooms/{room}/relations/{urllib.parse.quote(root, safe='')}/m.thread?dir=f&limit=100")
        count = 0
        for event in thread["chunk"]:
            if event["sender"] == self.live["userId"] or event["type"] != "m.room.message":
                continue
            edits = self.matrix(
                f"/v1/rooms/{room}/relations/{urllib.parse.quote(event['event_id'], safe='')}/m.replace?dir=b&limit=1",
            )["chunk"]
            content = edits[0]["content"].get("m.new_content", edits[0]["content"]) if edits else event["content"]
            count += token in str(content.get("body", ""))
        return count

    def run(self, token: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Return the workflow run whose source holds *token*, with its nodes in call order, once it is stored."""
        found = sorted((self.directory / "storage").glob("**/workflow_runs/workflow_runs.sqlite3"))
        if not found:
            return None
        with contextlib.closing(sqlite3.connect(f"file:{found[0]}?mode=ro", uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            run = connection.execute("SELECT * FROM workflow_runs WHERE instr(source, ?) > 0", (token,)).fetchone()
            if run is None:
                return None
            nodes = connection.execute("SELECT * FROM workflow_nodes WHERE run_id = ? ORDER BY seq", (run["run_id"],))
            return dict(run), [dict(node) for node in nodes]

    def ended_run(self, token: str, timeout: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Wait until the run of *token* reached a terminal state and return it with its nodes."""

        def ended() -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
            found = self.run(token)
            return found if found is not None and found[0]["state"] in _ENDED_RUN_STATES else None

        return until("the end of the run", ended, timeout, 1)


@dataclass
class Backend:
    """A deployment's backend process, which this script stops, kills and starts again as the stack started it."""

    deployment: Deployment
    pid: int
    command: list[str]
    directory: Path
    environment: dict[str, str]

    @property
    def running(self) -> bool:
        """Return whether the process this script last started or found still runs."""
        try:
            return str(self.deployment.directory) in Path(f"/proc/{self.pid}/cmdline").read_bytes().decode()
        except OSError:
            return False

    def stop(self) -> None:
        """Stop the backend the graceful way, as a deploy does: SIGTERM, then wait for it to leave."""
        os.kill(self.pid, signal.SIGTERM)
        until("the backend's graceful exit", lambda: not self.running, 120)

    def kill(self) -> None:
        """Kill the backend alone with SIGKILL, as a crash does; what it started is left for the next one to retire."""
        os.kill(self.pid, signal.SIGKILL)
        until("the backend's death", lambda: not self.running, 30)

    def survivors(self, group: int) -> list[int]:
        """Return the processes of the killed backend's group *group* that still run from this slot."""
        found = []
        for stat in Path("/proc").glob("[0-9]*/stat"):
            with contextlib.suppress(OSError, ValueError):
                # The fields after the command's closing parenthesis: state, parent, process group.
                in_group = int(stat.read_text().rsplit(")", 1)[1].split()[2]) == group
                if in_group and str(self.deployment.directory) in (stat.parent / "environ").read_bytes().decode():
                    found.append(int(stat.parent.name))
        return found

    def start(self, **extra: str) -> None:
        """Start the backend with its first command and environment plus *extra*, and wait for its API."""
        environment = {name: value for name, value in self.environment.items() if name != "MINDROOM_WORKFLOW_FAULT_DIR"}
        with Path(self.deployment.state["logs"]["backend"]).open("ab") as log:
            process = subprocess.Popen(  # the command the stack itself started
                self.command,
                cwd=self.directory,
                env={**environment, **extra},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.pid = process.pid
        # `just live down` stops the process the state file names.
        state = self.deployment.state
        state["pids"]["backend"] = self.pid
        (self.deployment.directory / "state.json").write_text(json.dumps(state, indent=1))
        until("the backend's API", self._healthy, 240, 1)

    def _healthy(self) -> bool:
        if not self.running:
            msg = f"the backend exited while starting; see {self.deployment.state['logs']['backend']}"
            raise LiveError(msg)
        with contextlib.suppress(OSError):
            url = f"{self.deployment.state['apiUrl']}/api/health"
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310  # the backend's API on 127.0.0.1
                return response.status == 200
        return False


def add_send_options(parser: argparse.ArgumentParser) -> None:
    """Add the options that say how the script's one message reaches the lobby."""
    parser.add_argument("--send", choices=["ask", "api"], default="ask", help="`just live ask`, or the Matrix API")
    parser.add_argument("--dev", type=Path, default=Path("~/repos/dev").expanduser(), help="where `just live` runs")


def start_run(deployment: Deployment, source: str, token: str, dev: Path | None) -> str:
    """Save *source* in the Helper's workspace, ask the Helper to start it, and return the thread's root event."""
    name = f"workflow_{token}.py"
    deployment.workspace.mkdir(parents=True, exist_ok=True)
    (deployment.workspace / name).write_text(source)
    root = deployment.ask(
        f'Call the tool start_workflow with path="{name}" and no other argument. Call it once and only once. '
        "Reply with the run ID it returns. When the run's result reaches you later, repeat that result word for word.",
        dev,
    )
    until("the workflow run", lambda: deployment.run(token), 120, 1)
    return root


def counter_lines(deployment: Deployment, name: str) -> int:
    """Count the lines of the file the run's shell wrote, wherever under the slot its working directory is."""
    return sum(len(path.read_text().splitlines()) for path in deployment.directory.glob(f"storage/**/{name}"))


def node_state(run: dict[str, Any], node: dict[str, Any]) -> str:
    """Name a call's state, with `cached` for a completed call that a later attempt read back without running it."""
    reused = node["state"] == "completed" and node["generation"] == 1 and node["attempt"] == run["attempt"] > 1
    return "cached" if reused else str(node["state"])


def main() -> int:
    """Run the fault once and print its result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--barrier", choices=sorted(_EXPECTED), required=True)
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    parser.add_argument("--settle", type=float, default=90, help="seconds to wait for reports after the run ended")
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    counter = f"counter_{token}.txt"
    # Only the run's result holds this word: the file's name and the call do not, so no other reply repeats it.
    report = f"harbor{secrets.token_hex(6)}"
    faults = deployment.directory / f"faults_{token}"
    faults.mkdir()
    barrier = _BARRIERS[args.barrier]
    if barrier == "before_settle":
        (faults / "before_receipt.release").touch()

    backend = deployment.backend()
    say(f"slot {deployment.slot}: restarting the backend with the {barrier} barrier")
    deployment.give_workflow_toolkit()
    backend.stop()
    backend.start(MINDROOM_WORKFLOW_FAULT_DIR=str(faults))
    root = start_run(deployment, _WRITE_ONCE.format(counter=counter, report=report), token, dev)
    until(f"the {barrier} barrier", (faults / f"{barrier}.reached").exists, 300)
    say(f"the write stands at {barrier}; killing the backend (process {backend.pid})")
    killed = backend.pid
    backend.kill()
    backend.start()
    say("the backend is back without the barrier; waiting for the run to end")
    run, nodes = deployment.ended_run(token, 600)
    time.sleep(args.settle)
    say(f"processes of the killed backend still running: {backend.survivors(killed) or 'none'}")
    reports = deployment.replies_with(root, report)
    line = f"lines={counter_lines(deployment, counter)} node={node_state(run, nodes[0])} reports={reports}"
    print(line, flush=True)
    say(f"run {run['run_id']} ended {run['state']} on attempt {run['attempt']}; expected `{_EXPECTED[args.barrier]}`")
    return 0 if line == _EXPECTED[args.barrier] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
