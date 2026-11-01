# One team memory store

This record holds the decisions behind keeping each team memory scope once, in team memory under `team_memory/`, instead of a copy in every member's storage, and behind the one-time merge of those copies.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- A team scope is still its member set (`team_<sorted members>`), not its team name, so which conversations share team memory does not change: two configured teams with the same members share one scope, and renaming a team keeps its memory.
  Team memory lives in `team_memory/`, apart from the team's conversation database under `teams/`, which is keyed by team name: every team's Mem0 records share one store there, each team's records kept apart by its scope ID, and each team's file memory has its own directory `team_memory/<team scope>/`.
  The directory name is the one earlier releases gave a team's file copies, so each copy moves to the directory its team reads; it drops a trailing underscore, so two teams whose member lists differ only there share a directory, as their file copies did.
  A name past 255 bytes is shortened and ends in a hash of the whole ID, because a large team's member list can pass the directory name limit.
- Each team fact is one record in one store, so a memory ID names one record and an edit or delete changes only that record.
  The copies were the second owner of every team fact: each member's Mem0 add ran its own extraction, and Mem0 copies an add's metadata onto every fact it extracts, so the replica key that was meant to name one fact named the whole turn, and editing one team fact through one member rewrote every fact of that turn in the other members' stores.
- A member reads every configured team it belongs to through its own backend, with no filter on the team's current backend, because a teammate switching backend changes where the team's new memories go, not whether the member may read the older ones.
- Every team's Mem0 records share one store, so a recall builds at most two Mem0 instances, the agent's store and the team store, however many teams the agent belongs to.
  Each Mem0 instance builds its own embedder, a new HTTPS client for an API embedder and a model copy for a local one, so a store per team cost every recall one connection setup or model load per team (measurements under Rejected alternatives).
  A shared store trades away the confinement a store per team gave: one corrupt team store reaches every team's Mem0 memory, and its collection's embedding size, which its first record sets, binds every team; the merge's size guard (Merge decisions) keeps a planted record from setting it.
  A member's get, update, and delete accept a record in the team store only when its scope ID is one of the member's teams, so a member never reaches another team's records in the shared store.
- The team store is read only when it exists, so recalling memory never creates an empty Chroma store.
  Mem0 searches the agent's store and then each of the agent's team scopes in the team store and stops at the first embedder failure, keeping the hits found so far: every search embeds the query with the same embedder, so after one failure the others would fail too, each after its own timeout.
- Search ranks every scope's hits together and does not merge equal texts across scopes, because two equal texts in two scopes are two records with their own IDs, and hiding one would make it reappear after the shown one is deleted.
- The `memory` tool's IDs stay unqualified: Mem0 IDs are UUIDs, file entry IDs carry a timestamp and 32 random bits, and `file:<path>:<line>` IDs come only from non-entrypoint Markdown, which only an agent's own workspace has; qualifying them would break every ID a model has already seen in a conversation.
- `memory.team_reads_member_memory` and `memory.file.path` are dropped before validation rather than rejected, because the dashboard wrote both on every save; neither ever changed behavior, since nothing read memory for a whole team and the file path applied only to an empty member list.
- The merge runs before anything serves, as the Migration Policy requires: in `mindroom run` under the runtime lock after old state-root worker mounts are retired, the one process that runs agent turns and memory tools.
  It runs right after the startup config loads, which names the agents whose Mem0 stores it reads (below); each copy's scope ID names its target team, so copies of teams the config no longer has merge too.

## Merge decisions

- File copies share each entry's ID across members, so entries union by ID.
  Only copies that hold what the runtime wrote there (one `MEMORY.md` of the header, blank lines, and `[id=...]` entries) are merged; any other copy stays where it is with a warning, because merging by line could drop or reorder hand-written content, and deleting what the merge did not read would lose it.
  Nothing is moved as a directory, so nothing the merge did not recognize reaches the primary-owned team root.
- Mem0 copies are separate extractions of one turn, so a replica key names a turn, not a fact, and no rule can pair one member's fact with another's without matching their text.
  The merge keeps one extraction per turn, the one holding the most distinct facts (Mem0's hash of each fact's text), and among extractions holding as many, the one from the member whose name sorts first, and every turn one member alone holds, so merged memory looks as if each team turn had been extracted once, as new turns are.
  The team store then keeps one record per fact per scope, as Mem0 keeps one per scope, because the extractions kept for two turns can repeat a fact each member's Mem0 store had deduplicated on its own.
  Counting facts, not taking the first member alone, matters because main's edit bug damaged the other copies: an edit through one member rewrote every fact of that turn in the other members' copies to the edited text, and a delete removed the whole turn there, so a damaged copy holds fewer distinct facts than the editing member's (recorded from main in `tests/fixtures/legacy_team_memory/mem0_member_stores_after_edits.json`).
  Keeping every member's extraction was tried first: in a live run with a real memory model, both members extracted every turn with the same facts in different words, so the merged store held each fact twice, and after one member edited the budget, the other member's answer reported both the old and the new budget.
  The cost is a fact only a dropped extraction found, which its member saw before the upgrade and no longer does.
- Kept records move with their IDs, embeddings, and metadata, so the merge makes no memory-model call, embeds nothing again, and every kept ID a model may still cite stays valid; the IDs of the dropped extractions stop resolving.
- Every team's records share one collection, whose embedding size its first record sets, so a record of another size would make the team store refuse every other team's records.
  The merge takes the size from records the primary-owned team store already holds, else, for OpenAI's own API, from the embedder's settings (its `dimensions`, or the model's own size), else from the embedder's answer to one embedding at the first start, and never from a member store, which worker code can write; a record of another size stays where it is with a warning.
  Settings vouch for the size only at OpenAI's own API, because no other provider receives `dimensions` and a compatible server, set as `host` or through `OPENAI_BASE_URL`, may ignore it or serve another model under an OpenAI name; trusting them elsewhere would refuse every valid record and then write the receipt.
  Settings come before asking, so the default OpenAI embedder never needs a request at the first start and an outage of it holds no merge back.
  Only the configured embedder's collection merges: the runtime read no other, and nothing trusted vouches for another collection's size.
  While an embedder that must be asked cannot answer, the Mem0 copies wait for a later start, as they do while Chroma cannot be installed.
  Asking has no deadline: building the Mem0 instance can install the local embedder's packages, download its model, or have Ollama pull a missing model, and a deadline would cut that work short and defer the merge into the window below (Accepted limitations); an embedder that accepts a request and never answers holds the first start until it is fixed.
- The Mem0 merge reads only the stores the runtime itself opens below `agents/`, those of configured shared agents on Mem0, so it runs after the config loads: no earlier release read team copies from any other store, and opening one would expose a path the runtime does not open (the static-runner sidecar can write below `agents/`).
- Each of these stays where it is with a warning, like the conversation conversion's handling of a worker-writable database, because one broken store below `agents/` must not stop startup: a member store Chroma cannot read or one holding links, a team's records the team store rejects as invalid (as a planted record with malformed metadata makes them), and a copy Chroma or the filesystem refuses to delete.
  Any other failure writing the team store, such as a storage error, stops the start before any Mem0 copy is deleted, so the next start merges the rest instead of leaving that team's memories behind for good.
  None of them merges on a later start: that would need a ledger of the turns already merged, because the team store cannot tell which merged facts a user edited or deleted since, and merging the rest later would bring a deleted fact back, undo an edit, or add a second extraction of a turn.
- A replica key that is not text, which only worker code could plant, makes its record a turn of its own, and of records sharing an ID, which only planting makes and Chroma refuses in one write, the team store takes the first, so a malformed record cannot stop startup.
  A member copy leaves its store only once team memory holds its fact for its scope, so a record whose ID a planted one took first stays in its member's store, out of team memory but never lost.
- Every step is idempotent: records are upserted by their IDs, member copies are deleted only after the team store holds every team's records, and the dropped extractions go before the kept ones, which go only once every dropped one went, so a start interrupted anywhere still finds each kept extraction beside any dropped one and chooses it again; writes go in batches within Chroma's batch limit, which needs no atomicity across batches.
- The merge reads the paths the earlier runtime read, walked without following links from the storage root: a team's copies only from its members, so a copy in another agent's directory or store, which no member read, does not become team memory.
  Chroma opens a store's files by path, so the merge opens a member's Mem0 store only when it holds nothing but directories and regular files; hard links are allowed, because a backup made with hard links gives every file a second link, and worker code can link only files it can already write.
- A receipt in `tracking/` stops later starts from opening every agent's Chroma store; it is withheld while Chroma cannot be installed or the embedder cannot answer, the only cases in which no Mem0 copy was merged, so a later start merges the Mem0 copies.
  The merge installs Chroma itself when a member Mem0 store exists, as the first memory call would, because merging later would hide old team memories until another restart.

## Rejected alternatives

- Team name as the scope identity, beside the team's conversations: it changes which conversations share memory and makes the merge depend on config.
- Keeping the copies with a per-fact replica key: every team turn would still run one extraction per member, and the copies would still diverge.
- Merging lazily when a team store first opens: it would run while replies are active.
- Re-adding facts through Mem0 `add(infer=False)`: it embeds every fact again at startup and assigns new IDs.
- Moving the first member's copy directory into place: it promotes whatever else that directory holds into a primary-owned root.
- Merging Mem0 copies from every agent store below `agents/`, without config: it opens stores of removed agents, file-memory agents, and directories worker code made, which the runtime never opens, for copies no release read.
- Keeping every member's extraction of a turn, or every one minus exact text duplicates: near-duplicate facts in different words survive, and editing one leaves its twin (see Merge decisions).
- One Mem0 store per team scope, under `team_memory/<team scope>/`: it confined a bad or planted record to its team, but every recall built one Mem0 instance, and so one embedder client or model copy, per team store.
  Measured warm recall median for an agent in 1, 2, and 4 teams, 50 records per scope, on a loaded shared host: with the OpenAI embedder (5 processes of 15 recalls) main took 509 to 522, 760 to 787, and 1242 to 1258 ms, a store per team 606, 1008, and 3449 ms, and the shared store 606, 983, and 1314 ms; with the local MiniLM embedder (3 processes of 10 recalls) main took 135, 173 to 208, and 171 to 182 ms at about 1044 to 1090 MB peak memory, a store per team 198, 347, and 471 ms at 1150, 1258, and 1475 MB, and the shared store 234, 237, and 322 ms at about 1250 MB.
  A store build alone costs about 22 ms with the OpenAI embedder and 66 ms with a warm local model; with the API embedder the per-team cost is the new HTTPS connection each instance's client opens.
- Caching built Mem0 instances per store, to keep a store per team: each cached instance keeps a resident copy of a local embedding model, so memory grows with agents plus teams, and instances shared across concurrent turns need an owner for eviction and credential refresh.
- Memoizing Mem0's embedder factory: `create_memory_instance` replaces Mem0's OpenAI embedder with MindRoom's strict one, so it misses the client recall uses.
- Searching the agent store and the team store concurrently: each Mem0 instance builds its own embedder, which for a local model holds a full copy of the model, so one recall would hold two copies at once.

## Accepted limitations

- One corrupt or unreadable team store affects every team's Mem0 memory, where a store per team confined it to one team.
- While the Mem0 copies wait for Chroma or for an embedder that must be asked, the runtime serves and writes new team memories, so the later merge adds the old copies beside them: a fact a member deleted or rewrote meanwhile can come back beside its new version.
  Failing startup instead would keep every agent down for memory alone, and holding team-memory writes back until the merge would need a gate on every team turn; settings that fix the size keep the default OpenAI embedder out of this window.
- Building the team store's Mem0 instance holds that store's lock, so concurrent recalls by members of different teams build it one after another, where a store per team built in parallel; a build takes tens of milliseconds warm, measured one recall at a time.
- Mem0 team copies an earlier embedder wrote stay in member stores, so switching back to that embedder does not bring them back.
- Mem0 copies in the store of an agent not on Mem0 at the upgrade stay unmerged, so switching it back to Mem0 later does not bring them back; merging such a store later would need a ledger of the turns already merged, since the team store cannot tell which merged facts users edited or deleted since.
- The merge checks a member's Mem0 store for links before Chroma opens it by path, so a link swapped in between is followed, as every Mem0 open of an agent's store below `agents/` already follows one.
- Each member's Mem0 history database stays where it is, because it also holds the member's own history; each team's recent-message extraction context in the team store starts empty once.
- Mem0's entity rows are not carried over; Mem0 writes them only when spaCy is installed, which MindRoom does not install.
- A release older than this one does not read `team_memory/`; after going back and upgrading again, the receipt must be deleted for the copies that release wrote to merge.
