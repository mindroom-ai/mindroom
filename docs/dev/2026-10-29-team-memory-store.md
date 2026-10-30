# One memory store per team scope

This record holds the decisions behind giving each team memory scope one store of its own instead of a copy in every member's storage, and behind the one-time merge of those copies.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- A team scope is still its member set (`team_<sorted members>`), not its team name, so which conversations share team memory does not change: two configured teams with the same members share one scope, and renaming a team keeps its memory.
  The scope's store lives at `team_memory/<team scope>/`, apart from the team's conversation database under `teams/`, which is keyed by team name.
- Each scope has exactly one store, so a memory ID names one record and an edit or delete changes only that record.
  The copies were the second owner of every team fact: each member's Mem0 add ran its own extraction, and Mem0 copies an add's metadata onto every fact it extracts, so the replica key that was meant to name one fact named the whole turn, and editing one team fact through one member rewrote every fact of that turn in the other members' stores.
- A member reads every configured team it belongs to through its own backend, with no filter on the team's current backend, because a teammate switching backend changes where the team's new memories go, not whether the member may read the older ones.
- Team stores are read only when they exist, so recalling memory never creates an empty Chroma store for a team that has none.
  Mem0 searches the agent's store and then its teams' stores one at a time and stops at the first embedder failure, keeping the hits found so far: every store embeds the query with the same embedder, so after one failure the others would fail too, each after its own timeout.
- Search ranks every scope's hits together and does not merge equal texts across scopes, because two equal texts in two scopes are two records with their own IDs, and hiding one would make it reappear after the shown one is deleted.
- The `memory` tool's IDs stay unqualified: Mem0 IDs are UUIDs, file entry IDs carry a timestamp and 32 random bits, and `file:<path>:<line>` IDs come only from non-entrypoint Markdown, which only an agent's own workspace has; qualifying them would break every ID a model has already seen in a conversation.
- `memory.team_reads_member_memory` and `memory.file.path` are dropped before validation rather than rejected, because the dashboard wrote both on every save; neither ever changed behavior, since nothing read memory for a whole team and the file path applied only to an empty member list.
- The merge runs before anything serves, as the Migration Policy requires: in `mindroom run` under the runtime lock after old state-root worker mounts are retired, and in the standalone API beside its other startup migrations, which run there without that retirement too. It needs no config, because each copy's scope ID names its target, so copies of removed teams merge too.

## Merge decisions

- File copies share each entry's ID across members, so entries union by ID.
  Only copies that hold what the runtime wrote there (one `MEMORY.md` of the header, blank lines, and `[id=...]` entries) are merged; any other copy stays where it is with a warning, because merging by line could drop or reorder hand-written content, and deleting what the merge did not read would lose it.
  Nothing is moved as a directory, so nothing the merge did not recognize reaches the primary-owned team root.
- Mem0 copies are separate extractions of one turn, so a replica key names a turn, not a fact, and no rule can pair one member's fact with another's without matching their text.
  The merge keeps one extraction per turn, the first member's by name, and every turn one member alone holds, so merged memory looks as if each team turn had been extracted once, as new turns are.
  Keeping every member's extraction was tried first: in a live run with a real memory model, both members extracted every turn with the same facts in different words, so the merged store held each fact twice, and after one member edited the budget, the other member's answer reported both the old and the new budget.
  The cost is a fact only a later member's extraction found, which that member saw before the upgrade and no longer does.
- Kept records move with their IDs, embeddings, and metadata, so the merge makes no embedder or memory-model call and every kept ID a model may still cite stays valid; the IDs of the dropped extractions stop resolving.
- A member store Chroma cannot read, or one holding links, is left where it is with a warning, like the conversation conversion's handling of a worker-writable database, because one broken store below `agents/` must not stop startup; its team memories are not merged on a later start either.
  Merging such a store after a repair would need a ledger of the turns already merged: the team's store cannot tell, because a user may have edited or deleted a merged turn's facts since, and merging the repaired copy would bring a deleted fact back or add a second extraction of a turn.
- A replica key that is not text, which only worker code could plant, makes its record a turn of its own, so a malformed record cannot stop startup.
- A team whose store refuses its records, as a planted record of another embedding dimension makes it, keeps every member's copies, and the receipt waits, so the merge runs again on the next start instead of stopping this one.
- Every step is idempotent: records are upserted by their IDs, member copies are deleted only after every team store holds its records, and the dropped extractions go before the kept ones, so a start interrupted anywhere still finds each kept extraction and chooses it again; writes go in batches within Chroma's batch limit, which needs no atomicity across batches.
- The merge reads the paths the earlier runtime read, walked without following links from the storage root.
  Chroma opens a store's files by path, so the merge opens a member's Mem0 store only when it holds nothing but directories and singly linked regular files; a store with a planted link stays unmerged, with a warning, because the static-runner sidecar can write below `agents/`, and the merge also opens the stores of agents the config no longer has.
- A receipt in `tracking/` stops later starts from opening every agent's Chroma store; it is withheld while Chroma cannot be installed, so a later start with Chroma merges the Mem0 copies.
  The merge installs Chroma itself when Mem0 copies exist, as the first memory call would, because merging later would hide old team memories until another restart.
- Both the runtime and the standalone API run the merge, because both run agent turns and memory tools.

## Rejected alternatives

- Team name as the scope identity, beside the team's conversations: it changes which conversations share memory and makes the merge depend on config.
- Keeping the copies with a per-fact replica key: every team turn would still run one extraction per member, and the copies would still diverge.
- Merging lazily when a team store first opens: it would run while replies are active.
- Re-adding facts through Mem0 `add(infer=False)`: it embeds every fact again at startup and assigns new IDs.
- Moving the first member's copy directory into place: it promotes whatever else that directory holds into a primary-owned root.
- Keeping every member's extraction of a turn, or every one minus exact text duplicates: near-duplicate facts in different words survive, and editing one leaves its twin (see Merge decisions).
- Searching the stores concurrently: each Mem0 instance builds its own embedder, which for a local model holds a full copy of the model, so one recall would hold one copy per team at once.
  Cost of searching one store at a time: each team store adds one Mem0 build and query to every recall, get, update, and delete of its members: about 50 ms warm with the OpenAI embedder, one more model load with a local sentence-transformers embedder, which Mem0 loads again for every instance, and one more request to the server with Ollama's.
- Caching Mem0 instances per store: it would need config invalidation and an owner for each instance's lifetime, for a saving only members of teams see.

## Accepted limitations

- The merge checks a member's Mem0 store for links before Chroma opens it by path, so a link swapped in between is followed, as every Mem0 open of an agent's store below `agents/` already follows one.
- Each member's Mem0 history database stays where it is, because it also holds the member's own history; a team store's recent-message extraction context starts empty once.
- Mem0's entity rows are not carried over; Mem0 writes them only when spaCy is installed, which MindRoom does not install.
- A release older than this one does not read `team_memory/`; after going back and upgrading again, the receipt must be deleted for the copies that release wrote to merge.
