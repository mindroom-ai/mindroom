---
icon: lucide/chart-column
---

# Widgets

An agent can draw a bar, line, area, or pie chart in its reply.
MindRoom Chat shows it as a card with a Chart and a Table view as soon as the agent has finished writing it; Element and other clients show the same numbers as a text table in a code block.

Charts are off by default.
Set `ui_widgets: true` on an agent, or under `defaults` for every agent that leaves it unset:

```yaml
agents:
  analyst:
    display_name: Analyst
    ui_widgets: true
```

The agent is then told how to write a chart, and draws one when you ask for a chart or want a trend or breakdown of numbers it has.
The setting applies from the agent's next reply, without a restart.
Only an agent's own replies draw charts: a team's reply, a message sent with the `matrix_message` tool, and the [OpenAI-compatible API](openai-api.md) show the agent's `ui` code block as it wrote it.

## Limits

- A chart holds at most 8 series and 200 points; for more, the agent answers in prose.
- A text table of more than 40 rows shows its first and last 20.
- MindRoom Chat shows the text table instead of the chart when the reply is too long for one ordinary message (see [Large Messages](streaming.md#large-messages)) or holds more than 8 charts.
- MindRoom Chat draws charts only for accounts named `mindroom_...` on your own homeserver, so keep that prefix reserved for MindRoom's agents.
