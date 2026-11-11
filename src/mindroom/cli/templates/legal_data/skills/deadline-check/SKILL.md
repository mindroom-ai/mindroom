---
name: deadline-check
description: List the person's upcoming legal deadlines and propose reminders
---

# Deadline check

Draft only.
This skill reports and proposes, and it creates reminders or changes memory only after the person approves each item.

## Collect

1. Every deadline under **Deadlines** in `MEMORY.md`.
2. Dates in each open matter's file in `memory/matters/` that are not yet listed there.
3. Existing reminders, from the scheduler's list of schedules.

## Report

- **Next 30 days:** soonest first, each with the date, what is due, the matter, the days left, and its source.
- **Later:** one line each.
- **Unclear:** deadlines whose date you could not confirm from a source, with what would settle it.
- **Passed without a record:** deadlines that have passed and have no outcome noted in the matter's file.

## Propose

- **Reminders:** for each deadline without one, when it should fire, such as a week and a day before.
- **Memory updates:** deadlines to add, correct, or remove, listed so the person can approve each one.

Apply only what the person approves.
