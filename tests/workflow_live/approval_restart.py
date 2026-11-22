"""Restart a live backend twice while an agent of a workflow run waits for an approval, and count what happened.

Usage, from the checkout, after `just live up --backend <checkout>` in `~/repos/dev` (README.md has every step):

    python3 -m tests.workflow_live.approval_restart [--slot N]

The run has one agent call.
The agent appends one line to a file with its shell tool and then calls `save_file`, which the script's config edit
makes ask for an approval.
While the card is open the script stops the backend with SIGTERM and starts it again, waits until the agent's call
shows `waiting` again, kills the backend with SIGKILL and starts it again, and only then approves the card.
When the run has ended the script prints

    lines=<lines in the file> cards=<approval cards the agent's call got> open=<cards open after both restarts>
    saved=<lines of the file the approved call wrote> agents=<completed agent calls>/<agent calls>
    attempt=<attempts of the run> run=<how the run ended> deliveries=<follow-up turns that delivered the run's job>
    reports=<replies with its result>

Expected: `lines=1 cards=1 open=1 saved=1 agents=1/1 attempt=3 run=completed deliveries=1 reports=1`.
It exits 0 when the line was written once, the one card survived both restarts and its approval was accepted, the
agent's call completed as the call it first was, and one reply carries the run's result.
Standard library only; Linux only.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import urllib.parse

from tests.workflow_live.kill_after_write import (
    Deployment,
    LiveError,
    add_send_options,
    counter_lines,
    outcome,
    say,
    start_run,
    until,
)

_ONE_AGENT = """\
from mindroom.workflow import agent


def main(args):
    agent(
        "Run the shell command `echo written >> {counter}` exactly once. Then call the tool save_file with "
        "file_name \\"{saved}\\" and contents \\"approved\\". Then reply with the single word: done. "
        "Never run the echo command a second time, whatever happens.",
        name="writer",
    )
    return {{"report": "{report}"}}
"""
_RULE = "    - match: save_file\n      action: require_approval\n"
_CARD = "io.mindroom.tool_approval"
_EXPECTED = "lines=1 cards=1 open=1 saved=1 agents=1/1 attempt=3 run=completed deliveries=1 reports=1"


def _require_approval(deployment: Deployment) -> bool:
    """Make `save_file` ask for an approval in the slot's config, and return whether the config changed."""
    path = deployment.directory / "config.yaml"
    text = path.read_text()
    if _RULE in text:
        return False
    # The section's own lines: everything indented under its key, whatever else the section sets.
    section = re.search(r"^tool_approval:\n((?:[ \t]+.*\n|\n)*)", text, re.MULTILINE)
    if section is None:
        text += f"\ntool_approval:\n  rules:\n{_RULE}"
    elif (rules := re.search(r"^  rules:\n", section.group(1), re.MULTILINE)) is not None:
        # First in the list: the first rule that matches a call decides.
        at = section.start(1) + rules.end()
        text = text[:at] + _RULE + text[at:]
    else:
        text = text[: section.start(1)] + f"  rules:\n{_RULE}" + text[section.start(1) :]
    path.write_text(text)
    return True


def _cards(deployment: Deployment, token: str) -> dict[str, str]:
    """Return the approval cards of the lobby whose call names *token*, by event ID, each with its newest status."""
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    found = {}
    for event in deployment.matrix(f"/v3/rooms/{room}/messages?dir=b&limit=100")["chunk"]:
        content = event.get("content", {})
        edit = content.get("m.relates_to", {}).get("rel_type") == "m.replace"
        if event["type"] != _CARD or edit or token not in json.dumps(content.get("arguments")):
            continue
        card = urllib.parse.quote(event["event_id"], safe="")
        edits = deployment.matrix(f"/v1/rooms/{room}/relations/{card}/m.replace?dir=b&limit=1")["chunk"]
        newest = edits[0]["content"].get("m.new_content", edits[0]["content"]) if edits else content
        found[event["event_id"]] = str(newest.get("status"))
    return found


def _approve(deployment: Deployment, card: str) -> None:
    """Approve *card* as the test account, as the button on the card does."""
    room = urllib.parse.quote(deployment.live["lobby"]["roomId"], safe="")
    answer = {"status": "approved", "m.relates_to": {"m.in_reply_to": {"event_id": card}}}
    deployment.matrix(f"/v3/rooms/{room}/send/{_CARD}_response/{secrets.token_hex(8)}", answer)


def _waiting_in_attempt(deployment: Deployment, token: str, attempt: int) -> bool:
    """Return whether the run is in *attempt* by now and its agent call shows that it waits for a person."""
    found = deployment.run(token)
    if found is None or found[0]["attempt"] < attempt:
        return False
    return any(node["kind"] == "agent" and node["state"] == "waiting" for node in found[1])


def main() -> int:
    """Run the two restarts once and print the result line; exit 0 when it is the expected one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--slot", type=int, help="the live deployment's slot (default: the only ready one)")
    add_send_options(parser)
    parser.add_argument("--settle", type=float, default=90, help="seconds to wait for reports after the run ended")
    args = parser.parse_args()
    dev = args.dev if args.send == "ask" else None
    deployment = Deployment.find(args.slot)
    token = f"tide{secrets.token_hex(6)}"
    counter, saved = f"counter_{token}.txt", f"approved_{token}.txt"
    # Only the run's result holds this word: no prompt and no agent's answer does, so no other reply repeats it.
    report = f"harbor{secrets.token_hex(6)}"

    backend = deployment.backend()
    changed = [deployment.give_workflow_toolkit(), _require_approval(deployment)]
    # A running backend reloads the edit some time later; one started after it has the rule before it is asked.
    if any(changed) or "MINDROOM_WORKFLOW_FAULT_DIR" in backend.environment:
        backend.stop()
        backend.start()
    root = start_run(deployment, _ONE_AGENT.format(counter=counter, saved=saved, report=report), token, dev)

    until("the agent's line", lambda: counter_lines(deployment, counter) or None, 600)
    until("the open card", lambda: "pending" in _cards(deployment, saved).values() or None, 300, 1)
    say(f"the card is open; restarting the backend (process {backend.pid})")
    backend.stop()
    backend.start()
    until("the agent's call to wait again", lambda: _waiting_in_attempt(deployment, token, 2) or None, 300, 1)
    say(f"the agent waits again; killing the backend (process {backend.pid})")
    backend.kill()
    backend.start()
    until("the agent's call to wait again", lambda: _waiting_in_attempt(deployment, token, 3) or None, 300, 1)
    cards = _cards(deployment, saved)
    still_open = [card for card, status in cards.items() if status == "pending"]
    say(f"cards after both restarts: {cards}")
    for card in still_open:
        _approve(deployment, card)
    run, nodes = deployment.ended_run(token, 900)
    time.sleep(args.settle)
    agents = [node for node in nodes if node["kind"] == "agent"]
    completed = sum(node["state"] == "completed" and node["generation"] == 1 for node in agents)
    line = (
        f"lines={counter_lines(deployment, counter)} cards={len(_cards(deployment, saved))} open={len(still_open)} "
        f"saved={counter_lines(deployment, saved)} agents={completed}/{len(agents)} {outcome(deployment, run)} "
        f"reports={deployment.replies_with(root, report)}"
    )
    print(line, flush=True)
    return 0 if line == _EXPECTED else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveError as error:
        say(str(error))
        sys.exit(1)
