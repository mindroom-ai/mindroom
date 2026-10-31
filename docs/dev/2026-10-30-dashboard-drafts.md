# Dashboard drafts: one store, complete tool entries

This record holds the decisions behind making the dashboard's zustand store the only owner of agent, team, room, memory, and knowledge base drafts, with complete tool entries as the draft's only tool state.

## Decisions

- The editors render from the store entity and write through store actions; no editor keeps a form copy.
  React Hook Form came with the original dashboard to sync form and store through a watcher, that watcher was removed because it caused re-render loops, and what stayed only mirrored the store and reset itself after every edit.
  The room, memory, and knowledge base editors kept the same kind of copy in component state with an effect that copied the store back after every write.
- A local text buffer is kept only where unfinished input cannot be a valid value yet, such as a compaction threshold percent while `0.` is typed, and tool override fields in the tool settings panel.
- `agents.<id>.tools` and `defaults.tools` hold complete authored entries in the draft, and widgets derive names from them.
  The load response always uses the compact entry forms (a bare name or a single-key mapping), because a dashboard save writes the validated config's authored dump, so keeping entries verbatim in the draft changes no saved file.
- Unchecking a tool, or removing it from `defaults.tools`, discards its settings; checking it again starts from a bare entry.
  The draft is exactly what a save writes, so the editor never shows settings that a save would drop.
- An override edit is a merge patch that rewrites only the keys it names, so values the settings panel does not manage stay as authored; it never adds a tool the entries do not list.
- A room's display name stays in the draft as typed, and saving trims it, as loading does, so typing a space between words works.
  A blank name still removes the authored name and shows the default name, because keeping a blank name would turn a derived room into an authored one.
- Every dashboard request addresses the dashboard's own origin under `/api`.
  `VITE_API_URL` was honored by only part of the dashboard, config load and save among the parts that ignored it, nothing in the repository set it, and development reaches the backend through the Vite proxy.

## Rejected alternatives

- Keeping the form as the draft owner and writing the store only on save: the draft must survive switching editors and is shared with Settings, Rooms, and the save bar, and the store already owns dirty state, validation, and saving.
- A names-based store API that rebuilds entries from names: it keeps the names-versus-entries split inside the store, and every caller already has the entity's entries.
- Keeping an unchecked tool's settings until save so that checking it again restores them: it needs a second owner for the settings of unselected tools, which is the side map this change removes.
- Routing every request through a configurable base URL: more code for a setting nothing used.
