---
icon: lucide/bug
---

# Bug Reports

When something goes wrong in a conversation, such as an agent stopping halfway, a reply never arriving, or a tool failing, the person who fixes it needs to know exactly what happened.
Collecting that by hand (screenshots, room and message IDs, app diagnostics, server logs) is slow, and users rarely know what to include.
Bug reports make it one click for the user and give the administrator both sides of the story: what MindRoom Chat saw and what the MindRoom backend did.

This is meant for deployments with an administrator who can already read everything, such as a company or family install.
Users keep their privacy from each other: a report is visible only to the person who sent it and the administrators at the time it was sent, and an administrator removed later keeps the reports they already received.

## How it works

1. In MindRoom Chat, the user opens the menu on any message and chooses **Report a bug**.
2. MindRoom Chat collects the evidence and sends it to a private room shared only with the administrators, then opens that room so the user can add details.
3. The administrators' MindRoom Chat joins that room automatically, so nobody has to accept an invite.
4. The administrator runs [`mindroom debug-report`](cli.md#debug-report) on the attached file to collect the backend side of the same moment.
5. Both files go to whoever fixes the issue, for example a coding agent.

## Enable it

Add the administrators to the homeserver's `/.well-known/matrix/client` file:

```json
{
  "m.homeserver": { "base_url": "https://matrix.example.com" },
  "io.mindroom.bug_reports": { "admins": ["@admin:example.com"] }
}
```

Nothing changes in `config.yaml`.
Without this key, the menu item is called **Download bug report** and saves the report as a file instead.

## What a report contains

- The room, thread, and message it was sent from, with a link.
- The conversation as MindRoom Chat holds it: the thread root and its newest 200 replies, or the last 50 messages when the message is not in a thread, each with its original content, latest edit, and send status.
- App details such as version, platform, browser, and connection state.
- The same data as **Export diagnostics** under **Settings → About**.

Nothing is redacted, and reports from encrypted rooms are stored decrypted in the report room.

## Where reports go

- Each user gets one private room named `Bug reports · <name>`, and each report is a thread in it, so follow-up questions stay next to the report.
- The room is unencrypted so administrator tools such as `matrix-mcp` can read it.
- Administrators are joined automatically only when they run MindRoom Chat and the invite is for a private report room created by someone on their own homeserver; other accounts, such as bots, accept the invite themselves.
- When an administrator is removed from the list, the next report goes to a new room; reports they already received stay with them.

## Collect the backend side

The report carries the IDs the backend uses, so one command on the MindRoom host gathers the agent runs, tool calls, model requests, deliveries, and log lines for that conversation:

```bash
mindroom debug-report mindroom-bug-report-<time>.json --output backend-report.json
```

Enable `debug.log_llm_requests` in advance if you also want full model requests and successful tool calls in it.
See [`mindroom debug-report`](cli.md#debug-report) for every option and data source.
