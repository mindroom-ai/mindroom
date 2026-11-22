"""Run 200 agents in an encrypted room on a live backend, and count what the run showed the room.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.graph_frames [--slot N] [--agents 200]

The script sets `room_defaults.encrypted: true` in the slot's config, with which the backend makes the lobby an
encrypted room when it starts: the test account has no right to, and the room's creator does it for its managed rooms.
It starts the backend with a capture directory, where the backend keeps each graph frame and each post and edit of
the run's card as it sent them: a script without a Matrix library cannot read them from an encrypted room.
The run is one `foreach` over that many agents, each of which answers with one word.
While it runs the script reads the room's unencrypted job state.
When the run has ended it saves the first snapshot, the first delta, the last snapshot and the final card content as
JSON files in the slot's log directory, for MindRoom Chat's fixtures, and prints

    agents=<completed agent calls>/<agent calls> encrypted=<the lobby is encrypted> refused=<frames not accepted>
    rate_limited=<rate-limit errors in the backend's log> state_jobs=<most jobs the room state listed at once>
    state_kinds=<kinds of the jobs it listed> state_labels=<listed jobs that carried an agent's label>
    card=<status of the final card> graph=<the final card has a graph> saved=<files written> run=<how the run ended>

Expected: `agents=200/200 encrypted=True refused=0 rate_limited=0 state_jobs=1 state_kinds=workflow state_labels=0
card=completed graph=True saved=4 run=completed`, on one line.
How many frames were sent depends on how long the agents take, so it is printed on stderr and is not in the line.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from tests.workflow_live.kill_after_write import Deployment, LiveError, add_send_options, say, start_run, until

_FAN_OUT = """\
from mindroom.workflow import agent, foreach, phase

# {token}: the word this script's run is found by.


def main(args):
    phase("Fan out")
    answers = foreach(
        list(range({agents})),
        lambda index: agent(f"Reply with the single word: done{{index}}", name=f"synthetic-{{index}}"),
    )
    return {{"report": "{report}", "answers": len(answers)}}
"""
_JOBS_STATE = "io.mindroom.background_jobs"


def _encrypted_rooms(text: str) -> str:
    """Return the config *text* with `room_defaults.encrypted: true`, which makes the backend encrypt its rooms."""
    if "\n  encrypted: true\n" in text:
        return text
    if "\nroom_defaults:\n" in text:
        return text.replace("\nroom_defaults:\n", "\nroom_defaults:\n  encrypted: true\n", 1)
    return text + "\nroom_defaults:\n  encrypted: true\n"


def _listed_jobs(deployment: Deployment) -> list[dict[str, Any]]:
    """Return every job the room's state lists now, for whichever bot."""
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    state: Any = deployment.matrix(f"/v3/rooms/{room}/state")
    return [job for event in state if event["type"] == _JOBS_STATE for job in event["content"].get("jobs", [])]


def _lines(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _save(directory: Path, frames: list[dict[str, Any]], card: dict[str, Any]) -> int:
    """Write the run's first snapshot, first delta, last snapshot and final card there; return how many it wrote."""
    snapshots = [frame for frame in frames if "snapshot" in frame]
    deltas = [frame for frame in frames if "delta" in frame]
    keep = {
        "workflow_frame_first_snapshot.json": snapshots[:1],
        "workflow_frame_first_delta.json": deltas[:1],
        "workflow_frame_last_snapshot.json": snapshots[-1:],
        "workflow_card_final.json": [card],
    }
    for name, found in keep.items():
        if found:
            (directory / name).write_text(json.dumps(found[0], indent=1))
    return sum(bool(found) for found in keep.values())


def main() -> int:
    """Run the fan-out once and print the result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--agents", type=int, default=200, help="how many agents the run starts")
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    report = f"harbor{secrets.token_hex(6)}"
    captured = deployment.directory / f"captured_{token}"
    captured.mkdir()
    log = Path(deployment.state["logs"]["backend"])

    backend = deployment.backend()
    deployment.give_workflow_toolkit()
    deployment.edit_config(_encrypted_rooms)
    say(f"slot {deployment.slot}: restarting the backend with the capture directory {captured}")
    backend.stop()
    logged_before = log.stat().st_size
    backend.start(MINDROOM_WORKFLOW_FAULT_DIR=str(captured))
    until("the backend to encrypt the lobby", deployment.lobby_encrypted, 120, 1)
    start_run(deployment, _FAN_OUT.format(token=token, agents=args.agents, report=report), token, dev)

    most, kinds, labels = 0, set(), 0

    def ended() -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        nonlocal most, labels
        jobs = _listed_jobs(deployment)
        most = max(most, len(jobs))
        kinds.update(str(job.get("kind")) for job in jobs)
        # Room state is not encrypted: an entry holds IDs only, never a label or a title.
        labels += sum("synthetic-" in json.dumps(job) for job in jobs)
        found = deployment.run(token)
        return found if found is not None and found[0]["state"] in ("completed", "failed", "cancelled") else None

    run, nodes = until("the end of the run", ended, 3600, 2)
    card_file, frame_file = captured / "workflow_cards.jsonl", captured / "workflow_frames.jsonl"

    def mine() -> list[dict[str, Any]]:
        return [entry for entry in _lines(frame_file) if entry["frame"]["run"] == run["run_id"]]

    def last_frame_sent() -> bool:
        # The last frame of a run is a snapshot that says how it ended.
        return any(
            "snapshot" in entry["frame"] and entry["frame"]["meta"]["status"] == run["state"] for entry in mine()
        )

    until("the final card", lambda: any(card["status"] == run["state"] for card in _lines(card_file)) or None, 60, 1)
    until("the snapshot of the run's end", last_frame_sent, 60, 1)
    frames, cards = mine(), _lines(card_file)
    final = [card for card in cards if card["run"] == run["run_id"]][-1]
    saved = _save(log.parent, [entry["frame"] for entry in frames], final)
    with log.open("rb") as written:
        written.seek(logged_before)
        rate_limited = written.read().count(b"M_LIMIT_EXCEEDED")
    agents = [node for node in nodes if node["kind"] == "agent"]
    completed = sum(node["state"] == "completed" and node["generation"] == 1 for node in agents)
    say(f"{len(frames)} frames, {len(cards)} card posts and edits; {saved} files saved in {log.parent}")
    line = (
        f"agents={completed}/{len(agents)} encrypted={deployment.lobby_encrypted()} "
        f"refused={sum(entry['outcome'] != 'sent' for entry in frames)} rate_limited={rate_limited} "
        f"state_jobs={most} state_kinds={','.join(sorted(kinds))} state_labels={labels} card={final['status']} "
        f"graph={'graph' in final} saved={saved} run={run['state']}"
    )
    print(line, flush=True)
    expected = (
        f"agents={args.agents}/{args.agents} encrypted=True refused=0 rate_limited=0 state_jobs=1 "
        "state_kinds=workflow state_labels=0 card=completed graph=True saved=4 run=completed"
    )
    return 0 if line == expected else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
