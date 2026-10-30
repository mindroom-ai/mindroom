---
name: status-report
description: Write a cited project status report from fresh sources
---

# Status Report

Use this skill when someone asks for project status or a scheduled digest runs.

## Gather

1. Re-read every source in `memory/sources.md` that bears on the report, and note each one you could not read or could read only in part.
2. Record what changed in `memory/tracker.md`, and log anything uncertain in `memory/questions.md`.
3. Open the newest report in `memory/reports/`, if there is one.

## Write

1. **As of:** the date, time, and timezone, the sources covered, and the sources you could not read.
2. **Summary:** at most three sentences.
3. **Workstreams:** for each one, list finished work, work underway, blocked items with the step that would free them, and the next step with its owner.
4. **Milestones:** baseline against forecast, with the evidence for any slip.
5. **New risks and open asks:** each with the Matrix ID of the person who needs to act, so they are notified.
6. **Since last report:** what moved, what is new, and what disappeared without being closed.

## Check

- Link every item to its source.
- An empty result means you found nothing, not that all is well; say where you looked.
- Use status colors only if the team has defined them, and apply the team's definitions.
- The last person to post about an item is not its owner; take owners from the tracker or an explicit assignment.

## Keep

Save the posted report as `memory/reports/YYYY-MM-DD-HHMM.md`, creating the folder if needed, so the next report can compare against it.
