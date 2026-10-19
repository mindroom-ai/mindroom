# Knowledge Graph

A knowledge graph gives an agent a model of the people, projects, documents, and events its conversations and tools reveal, with when each fact held and where it came from.
MindRoom builds it with [Graphiti](https://github.com/getzep/graphiti) in the background from the agent's finished conversations, and the agent looks facts up with the `knowledge_graph` tool.
To have the agent also explore its data sources on a schedule, add the [`exploration`](https://docs.mindroom.chat/scheduling/#exploration) automation.
[Memory](https://docs.mindroom.chat/memory/) keeps what is worth remembering as text and a [knowledge base](https://docs.mindroom.chat/knowledge/) searches your documents; the graph links the entities that conversations and tool results reveal, with dated facts, so an agent can answer who owns what, how things connect, and what changed.

## Enable It

```yaml
models:
  luna:
    provider: openai
    id: gpt-6-luna

knowledge_graph:
  model: luna          # extracts entities and facts

agents:
  mind:
    display_name: Mind
    knowledge_graph: true
```

`agents.<name>.knowledge_graph` (bool, default from `defaults.knowledge_graph`, which defaults to `false`) turns the graph on for one agent and gives it the `knowledge_graph` tool.
MindRoom installs the optional `knowledge_graph` extra the first time a graph is used; its embedded database ships for macOS and for Linux with glibc 2.39 or newer, and elsewhere it must build from source.
Extraction needs a model with reliable structured output; small local models often fail it; a failed extraction is retried later, and a part of a conversation is skipped once its extraction output was unusable five times or it kept failing for three days.

## Settings

| Field | Default | Description |
|---|---|---|
| `knowledge_graph.model` | none | A key of `models` that extracts entities and facts; required once any agent enables the graph |
| `knowledge_graph.extraction_instructions` | empty | Text with your own guidance for extraction, added after the built-in instructions, such as which topics, systems, or kinds of facts matter in your organization; it applies to what is added afterwards, or to the last `backfill_days` of conversations once you raise `knowledge_graph.generation` |
| `knowledge_graph.ingest.interval_seconds` | `600` (min 30) | Time to wait after an ingestion pass that added nothing; a pass that added something is followed at once |
| `knowledge_graph.ingest.idle_seconds` | `120` (min 1) | How long a conversation must be quiet before its new turns are added |
| `knowledge_graph.ingest.max_sessions_per_cycle` | `10` (min 1) | Conversations each graph adds per pass; the rest wait for the next |
| `knowledge_graph.ingest.max_episode_chars` | `24000` (1000 to 60000) | Largest piece of a conversation sent for extraction at once; a single longer message is cut to this length |
| `knowledge_graph.ingest.max_tool_result_chars` | `4000` (min 100, at most half of `max_episode_chars`) | Characters kept from each tool result |
| `knowledge_graph.ingest.backfill_days` | `7` (min 0) | A new graph starts from conversations active within this many days |
| `knowledge_graph.aliases` | `{}` | The one name the graph uses for a person, agent, or thing, mapped to its other names; see [Names](#names) |
| `knowledge_graph.generation` | `0` (integer, min 0) | Raise it to start every graph over without restarting MindRoom |

With the defaults, new turns reach the graph 2 to 12 minutes after a conversation goes quiet, plus extraction time, and later when many conversations are waiting.
Up to four agents' graphs are filled at the same time, counting each requester of a private agent separately, so the extraction model can receive requests for four graphs at once.
Facts are embedded with [`memory.embedder`](https://docs.mindroom.chat/memory/#embedder).
The graph database runs inside MindRoom and keeps every graph in memory, so MindRoom's memory use grows with the graphs.
Changing the embedder or raising `knowledge_graph.generation` starts new, empty graphs that fill again from conversations active within `backfill_days`, which pays for that extraction again, charged to each conversation's requester.
Replies and voice calls already running when the change applies keep reading the old graphs until they finish, and setting an earlier generation again reads that generation's graphs as they were left.
The old graphs keep their memory until you stop MindRoom and delete `knowledge_graph/` in the storage directory, which also starts the current graphs over.

## What Goes Into the Graph

- Every finished turn of the agent's Matrix conversations: what people said, what the agent answered, and the tool calls it made with their results.
- From [`exploration`](https://docs.mindroom.chat/scheduling/#exploration) threads, tasks another agent delegated to it, and an automation's turn inside an ordinary conversation, only the tool calls and their results, never the agent's own summary.

Not added: threads started by other automations such as `dreaming`, graph lookups, including those through a shell, the `reasoning` tool's thoughts, answers from subagents, workflows, and Claude Code, results collected from background jobs, conversations through the OpenAI-compatible API, team conversations, and context that hooks attach to messages, such as location.
Credential-like values are removed on a best-effort basis before anything is sent for extraction.
A turn waiting for tool approval holds back the later turns of its conversation until it finishes or its approval expires.

Extraction asks the model to sort what it finds into people, agents, organizations, locations, events, projects, products, documents, and accounts, to leave out what fits none of them, and to keep file paths, URLs, and the like inside the facts that mention them, so pets, foods, hobbies, and topics get no entity of their own.
It records what the agent reports as facts about their subjects and leaves out the agent's own lookups and housekeeping, such as what it searched, read, or saved, its access, accounts, tools, and configuration, and the counts and progress of its own tasks; what the agent did for others, such as filing an issue or sending a message, is kept.
This steers the model rather than filtering its output, so an occasional untyped entity, such as a file that only a fact names, or a fact about the agent's own activity can still appear.

Each fact keeps its sources: `conversation` means someone said it, `tool_results` means it came from a tool call, its arguments or what it returned.
When later evidence contradicts a fact, the fact is marked superseded with the date it stopped holding instead of being deleted.
Facts are extracted by a model, so they can be wrong; check a fact's source before relying on it.
A message redacted before its conversation is added stays out of the graph, and so do the turn that read it and every later turn its redaction removes from [history](https://docs.mindroom.chat/configuration/history/#compacted-history-and-redaction).
Redacting a message later does not remove facts already extracted from it; to stop agents from reading them, raise `knowledge_graph.generation`, or, to delete them too, instead stop MindRoom and delete `knowledge_graph/` in the storage directory, and either way the graphs rebuild from the remaining conversations of the last `backfill_days`, without redacted messages.

## Names

Each agent and person gets one name in the graph, so their facts collect on one entity instead of one per spelling.
An agent or team goes by its `display_name`, and a person by the newest display name their messages in the conversation carried, without emoji and other symbols, or else by their Matrix username.
Extraction is told the other names each one goes by, such as their Matrix ID, username, or agent key, and uses the one name for those too.
`knowledge_graph.aliases` gives one name to the other names of a person, agent, or thing:

```yaml
knowledge_graph:
  aliases:
    Alice Smith: ["@alice:example.org", Ali, alice@example.org]   # her Matrix account, nickname, and email
    Scout: [research]                                             # the agent whose key is research
    Project Atlas: [Atlas, Atlas v2]                              # something without an account
```

A Matrix ID or agent key in an entry gives that person or agent the entry's name everywhere, whatever display name they use; a display name never links an entry.
Someone else whose display name is the name of an entry with a Matrix ID or agent key goes by their username, so the two stay apart.
A speaker whose display name is the name of an entry without one shares that entry's entity and its other names, so an entry named after your own display name needs no Matrix ID.
A username that an entry with a Matrix ID or agent key, or an agent, is already named gets its homeserver added, such as `alice work.example`.
Every other alias is a plain name that extraction maps to the entry's name and that `graph_entity` finds the entity by.
Each alias belongs to one entry and is not another entry's name, in any letter case; names and aliases use single spaces, and a name cannot contain a colon.
Two people with the same display name, or a person named like an agent, share one entity unless an entry gives one of them a name through their Matrix ID.
Every entry is sent with every piece of a conversation for extraction, so a long list makes each extraction prompt longer.
Entities extracted earlier under other names, such as a Matrix ID, stay separate until you raise `knowledge_graph.generation`.

## The `knowledge_graph` Tool

```python
search_graph("who owns Project Atlas", limit=10)   # limit 1 to 50
graph_entity("Project Atlas")
```

The agent decides when to look facts up; ask it to search the graph, or to cite each fact's source and date, when you want to be sure.
Each fact comes back with its validity, for example `valid 2026-01-05 until 2026-03-09 (superseded)`, and its sources with dates.
`graph_entity` returns the entity whose name equals the given name in any letter case, and the one with the most facts when several do.
A name listed in [`knowledge_graph.aliases`](#names), the username of a Matrix ID listed there, or an agent's key, Matrix ID, or display name finds the entity under its one name, or the entity named exactly as asked when there is none under that name yet.
Without an exact name it returns the closest match and ends with `Other close matches: ...`, up to four more names to look up when the first is not the one meant.
An agent reads only its own graph; a [private agent](https://docs.mindroom.chat/configuration/agents/#private-instances) has one graph per requester.

## Privacy

A shared agent's graph holds what it learned from every requester's conversations, including tool results, and anyone who can talk to that agent can query it.
Use a private agent when requesters must not see each other's facts.

## Cost and Health

Extraction is reported as `knowledge_graph` in [usage tracking](https://docs.mindroom.chat/usage/#token-usage) and makes about 20 to 25 model calls per piece of a conversation.
Each lookup makes one embedding request.
When ingestion fails, `/api/health` includes a `knowledge_graph` block naming the kind of failure until a later pass succeeds with no conversation still waiting to retry a failed attempt, and the MindRoom log has the details.
