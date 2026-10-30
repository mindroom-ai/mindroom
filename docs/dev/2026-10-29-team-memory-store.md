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
- The merge runs before anything serves, as the Migration Policy requires: in `mindroom run` under the runtime lock after old state-root worker mounts are retired, and in the standalone API beside its other startup migrations, which run there without that retirement too. It runs right after the startup config loads, which names the agents whose Mem0 stores it reads (below); each copy's scope ID names its target team, so copies of teams the config no longer has merge too.

## Merge decisions

- File copies share each entry's ID across members, so entries union by ID.
  Only copies that hold what the runtime wrote there (one `MEMORY.md` of the header, blank lines, and `[id=...]` entries) are merged; any other copy stays where it is with a warning, because merging by line could drop or reorder hand-written content, and deleting what the merge did not read would lose it.
  Nothing is moved as a directory, so nothing the merge did not recognize reaches the primary-owned team root.
- Mem0 copies are separate extractions of one turn, so a replica key names a turn, not a fact, and no rule can pair one member's fact with another's without matching their text.
  The merge keeps one extraction per turn, the one holding the most distinct facts (Mem0's hash of each fact's text), the first member's by name among equals, and every turn one member alone holds, so merged memory looks as if each team turn had been extracted once, as new turns are.
  The team's store then keeps one record per fact, as Mem0 keeps one per scope, because the extractions kept for two turns can repeat a fact each member's Mem0 store had deduplicated on its own.
  Counting facts, not taking the first member alone, matters because main's edit bug damaged the other copies: an edit through one member rewrote every fact of that turn in the other members' copies to the edited text, and a delete removed the whole turn there, so a damaged copy holds fewer distinct facts than the editing member's (recorded from main in `tests/fixtures/legacy_team_memory/mem0_member_stores_after_edits.json`).
  Keeping every member's extraction was tried first: in a live run with a real memory model, both members extracted every turn with the same facts in different words, so the merged store held each fact twice, and after one member edited the budget, the other member's answer reported both the old and the new budget.
  The cost is a fact only a dropped extraction found, which its member saw before the upgrade and no longer does.
- Kept records move with their IDs, embeddings, and metadata, so the merge makes no embedder or memory-model call and every kept ID a model may still cite stays valid; the IDs of the dropped extractions stop resolving.
- The Mem0 merge reads only the stores the runtime itself opens below `agents/`, those of configured shared agents on Mem0, so it runs after the config loads: no earlier release read team copies from any other store, and opening one would expose a path the runtime does not open (the static-runner sidecar can write below `agents/`).
- A member store Chroma cannot read or one holding links, a team whose store refuses its records (as a planted record of another embedding dimension makes it), and a copy Chroma or the filesystem refuses to delete are left where they are with a warning, like the conversation conversion's handling of a worker-writable database, because one broken store below `agents/` must not stop startup.
  None of them merges on a later start: that would need a ledger of the turns already merged, because the team's store cannot tell which merged facts a user edited or deleted since, and merging the rest later would bring a deleted fact back, undo an edit, or add a second extraction of a turn.
- A replica key that is not text, which only worker code could plant, makes its record a turn of its own, so a malformed record cannot stop startup.
- Every step is idempotent: records are upserted by their IDs, member copies are deleted only after every team store holds its records, and the dropped extractions go before the kept ones, which go only once every dropped one went, so a start interrupted anywhere still finds each kept extraction beside any dropped one and chooses it again; writes go in batches within Chroma's batch limit, which needs no atomicity across batches.
- The merge reads the paths the earlier runtime read, walked without following links from the storage root.
  Chroma opens a store's files by path, so the merge opens a member's Mem0 store only when it holds nothing but directories and regular files; hard links are allowed, because a backup made with hard links gives every file a second link, and worker code can link only files it can already write.
- A receipt in `tracking/` stops later starts from opening every agent's Chroma store; it is withheld while Chroma cannot be installed or no config loaded, the only cases in which nothing was merged, so a later start merges the Mem0 copies.
  The merge installs Chroma itself when a member Mem0 store exists, as the first memory call would, because merging later would hide old team memories until another restart.
- Both the runtime and the standalone API run the merge, because both run agent turns and memory tools.

## Rejected alternatives

- Team name as the scope identity, beside the team's conversations: it changes which conversations share memory and makes the merge depend on config.
- Keeping the copies with a per-fact replica key: every team turn would still run one extraction per member, and the copies would still diverge.
- Merging lazily when a team store first opens: it would run while replies are active.
- Re-adding facts through Mem0 `add(infer=False)`: it embeds every fact again at startup and assigns new IDs.
- Moving the first member's copy directory into place: it promotes whatever else that directory holds into a primary-owned root.
- Merging Mem0 copies from every agent store below `agents/`, without config: it opens stores of removed agents, file-memory agents, and directories worker code made, which the runtime never opens, for copies no release read.
- Keeping every member's extraction of a turn, or every one minus exact text duplicates: near-duplicate facts in different words survive, and editing one leaves its twin (see Merge decisions).
- Searching the stores concurrently: each Mem0 instance builds its own embedder, which for a local model holds a full copy of the model, so one recall would hold one copy per team at once.
  Cost of searching one store at a time: each team store adds one Mem0 build and query to every recall, get, update, and delete of its members: about 50 ms warm with the OpenAI embedder, one more model load with a local sentence-transformers embedder, which Mem0 loads again for every instance, and one more request to the server with Ollama's.
- Caching Mem0 instances per store: it would need config invalidation and an owner for each instance's lifetime, for a saving only members of teams see.

## Accepted limitations

- Mem0 copies in the store of an agent not on Mem0 at the upgrade stay unmerged, so switching it back to Mem0 later does not bring them back; merging such a store later would need a ledger of the turns already merged, since the team's store cannot tell which merged facts users edited or deleted since.
- A standalone API whose config fails to load at startup merges Mem0 copies only at a later start with a valid config, not when a repaired config loads while it runs, because a merge must finish before anything reads team memory, and the API serves while the config is invalid.
- The merge checks a member's Mem0 store for links before Chroma opens it by path, so a link swapped in between is followed, as every Mem0 open of an agent's store below `agents/` already follows one.
- Each member's Mem0 history database stays where it is, because it also holds the member's own history; a team store's recent-message extraction context starts empty once.
- Mem0's entity rows are not carried over; Mem0 writes them only when spaCy is installed, which MindRoom does not install.
- A release older than this one does not read `team_memory/`; after going back and upgrading again, the receipt must be deleted for the copies that release wrote to merge.
