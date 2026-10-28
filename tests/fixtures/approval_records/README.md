# Approval-record recordings

These files are the rows the event journal wrote for each tool-approval state before the approval tables were replaced, so the one-time conversion is tested against real rows instead of hand-written ones.
`tests/approval_records_recorder.py` recorded them at commit `b19c4c4acd5ea2f3b8aa126ab55048b3ca41713a` by driving the approval manager, the response coordinator, and the journal's own transactions onto a fresh SQLite journal.

`schema.sql` is the schema those rows belong to, as `schema_statements(SQLITE_DIALECT)` rendered it at that commit.
Each `<scenario>.sql` holds every row of every journal table after one scenario, as `INSERT` statements in schema order, headed by a one-line description.
Load a recording by running `schema.sql` and then the scenario file against an empty SQLite database.

| File | State recorded |
| --- | --- |
| `01_waiting_card_acknowledged.sql` | A paused reply waiting for one card that Matrix acknowledged. |
| `02_waiting_card_unacknowledged.sql` | A paused reply waiting for one card whose send Matrix refused, so it has no event ID. |
| `03_waiting_policy_decided_and_card.sql` | A paused reply with one call the policy script approved without a card and one call waiting for its card. |
| `04_ready.sql` | A paused reply whose two calls were approved on their cards, ready and not claimed. |
| `05_claimed.sql` | A paused reply whose approved call a resume span claimed. |
| `06_failing.sql` | Failing paused replies for `cancelled_by_user` (a Stop on a waiting reply), `sync_restart_cancelled`, `interrupted`, and `INTERRUPTED_FAILURE_REASON` (claimed resumes), and an error text (a ready run). |
| `07_chained_generation.sql` | A resumed run that paused again, its second generation waiting for its own card. |
| `08_denied_edit_unacknowledged.sql` | Denied cards whose terminal edit Matrix has not acknowledged: one never attempted, one whose send was refused. |
| `09_retired_card.sql` | A denied card retired to its tombstone, its card and delivery rows gone. |
| `10_background_script_pending.sql` | A background script's call waiting for its card. |
| `11_tool_job_calls.sql` | A background job's call waiting for its card and another job's call that was denied, its card retired. |
| `12_scheduled_calls.sql` | Scheduled calls that are pending, approved for any arguments, armed, consumed with a completed outcome and a retired receipt, and withdrawn. |
| `13_timed_grants.sql` | A timed grant that approved a second waiting card in its batch and two later calls at reservation, one receipt live and one retired, and a revoked grant in another thread. |
| `14_expired_calls.sql` | A paused reply's call and a background job's call that the deadline sweep expired. |

The router's journal principal is `router@@mindroom_router:example.org`, at its second membership of the room (epoch 1), and the replying agent's is `code@@mindroom_code:example.org`.
Every recording starts its clock at 2027-01-15T08:00:00Z and moves it only between its steps, and the n-th `uuid4` it reaches is `UUID(int=n)`, so recording again at the same commit writes identical files.
Grant scope keys and operations hash the whole config model, so recording at another commit can change them even when nothing about approvals changed.

## Recording again

The recorder only describes a journal that still writes these tables, so record again only on a commit from before the replacement.
From the repository root, inside the NixOS development shell:

```bash
nix-shell shell.nix --run "env -u TMPDIR -u TEMP -u TMP MINDROOM_RECORD_APPROVAL_FIXTURES=1 uv run pytest tests/approval_records_recorder.py -n 0 --no-cov"
```

Without `MINDROOM_RECORD_APPROVAL_FIXTURES=1` every recorder test is skipped and nothing is written.
After writing each file, the recorder loads it into a fresh database, opens it with `EventJournalStore`, reads every continuation, card, grant, scheduled call, and detached call back, and checks that no row changed.
Update the commit named above when you record again.
