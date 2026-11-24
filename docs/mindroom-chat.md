---
icon: lucide/message-square
---

# MindRoom Chat

MindRoom Chat is the Matrix chat client built for MindRoom, with native views for threaded agent conversations, streaming replies, canvases, tool approvals, and voice calls.
Open it in a browser at [chat.mindroom.chat](https://chat.mindroom.chat), or self-host it against your own Matrix homeserver.
On macOS, open it inside the [macOS App](installation/macos-app.md) or install the standalone MindRoom Chat desktop app from that app's **Overview** screen.
On iPhone, iPad, and Apple silicon Macs, use the native client described in the [iOS App](ios.md) guide.

## Navigation and Layout

By default, the left rail shows **Home**, **Direct Messages**, **Activity**, your Matrix **Spaces**, and your account menu (and **Settings**) pinned at the bottom.
Optional rail buttons for **Threads**, **Canvases** (once at least one canvas exists), **Explore Community**, and **Create Space** are off by default, can be enabled in **Settings** > **Sidebar**, and stay reachable from Home's `⋮` menu while hidden.
The **Threads** view lists threads across rooms under **Mine**, **Following**, and **All**, grouped in order by **Needs you** (pending approvals and questions), **Unread** (including mentions), **Agents working**, **Recent**, **Later** (snoozed or scheduled threads), and **Automations** (under **All**).
The **Activity** bell shows mentions, replies, and items waiting on you.
Press `Cmd+\` (`Ctrl+\` on Linux and Windows) to show or hide the room list sidebar.

Inside a room, the header's **Compact view** toggle switches between **Compact** (thread cards with status, tag, and people filters) and **Threads** (room roots in timeline order with thread chips); **Jump to Time** opens a flat **Classic** timeline, from which **Compact view** returns to **Compact**.
Rooms and direct messages with an agent open in **Compact** by default (or **Threads** when the room's agents answer in the main timeline), rooms without agents default to **Threads**, and human-only direct messages stay in **Threads** with no view switcher.
Selecting a thread opens its dedicated thread view; press `Option+Right` (`Alt+Right` on Linux and Windows) to enter the focused thread and `Option+Left` (`Alt+Left`) to return to the room view.

Press `Cmd+K` (`Ctrl+K`) to open **Search and commands** and jump to rooms, threads, people, messages, spaces, or actions such as toggling light/dark theme or resolving the active thread.
Press `Cmd+Shift+F` (`Ctrl+Shift+F`) to open **Search and commands** on its **Messages** tab, then select **See all results** after typing a query to open **Message Search** with room and sort filters; Matrix homeservers cannot search encrypted rooms.

On desktop, rooms display one resizable right-hand side panel at a time beside the conversation; **Computer**, **Canvas**, **Sub-agents**, and a docked **Call** (or its live transcript) can fold to the edge or expand to fill the room, while **Members** toggles from the room header without that fold/fill frame.
On phones, side panels cover the conversation instead of sitting beside it, and calls stay in the room panel rather than floating.

- **Members** (`Cmd+Shift+M` / `Ctrl+Shift+M`) lists the people and agents in the room.
- **Computer** shows a live view of an agent's worker browser so you can watch or take control; see [Worker Computer](tools/worker-computer.md).
- **Canvas** displays interactive web pages an agent builds beside the conversation, while inline canvases render directly inside the thread; see [Interactive Canvases](canvases.md) and [Agent Chat UI Actions](tools/chat-ui.md).
- **Sub-agents** shows running and finished sub-agent turns in the current room or thread.
- **Call** (and its live transcript) floats in its own window or docks beside the room on desktop, and opens in the room panel on phones; see [Voice Calls](voice-calls.md).

## Composer and Message Formatting

The composer enables Markdown by default (toggle **Markdown Formatting** in **Settings** > **Chats**) and offers a **Formatting** toolbar for headings, bold, italic, underline, strikethrough, inline code, spoilers, block quotes, code blocks, and lists.
In the composer, you can write `**bold**`, `*italic*` or `_italic_`, `~~strikethrough~~`, `` `inline code` ``, fenced code blocks (```` ```lang ````), `||spoiler||`, links, headings, quotes, lists, tables, `$inline$` math (no spaces touching `$`), and `$$display$$` math on its own line.
Received replies also render `<details><summary>...</summary>...</details>` collapsibles, syntax-highlighted code blocks (with copy, line-wrap, and expand/collapse controls), scrollable tables, and KaTeX math.
Mermaid fences, GitHub callouts (`> [!NOTE]`), footnotes, and external `https://` image URLs are not rendered in chat Markdown.

Drag and drop files into the composer or select **Attach files** to send images, videos, audio, and files.
Pasting text large enough to exceed Matrix's event size budget automatically converts the paste into a text file attachment so long logs or snippets send cleanly.
Select **Record voice message** to record, pause, resume, or discard a voice message; see [Voice Messages](voice.md) for transcription setup.

Use a message's reply action to quote-reply in context, or press `Up` in an empty composer to edit your last message.
Inside a thread an agent answers in, the composer's **Choose model** picker sets a per-thread model override, and choosing its inherited default row (**Agent default**, **Room model**, or **Default**) clears the override; the `!model` command in [Chat Commands](chat-commands.md) does the same from any client.
On desktop, press `Enter` to send and `Shift+Enter` for a new line, or enable **ENTER for Newline** in **Settings** > **Chats** to send with `Cmd+Enter` (`Ctrl+Enter`).
On phones and tablets with a hardware keyboard, `Enter` inserts a new line and `Cmd+Enter` (iPhone/iPad) or `Ctrl+Enter` (Android) sends regardless of **ENTER for Newline**.

## Working with Conversations and Replies

- **Thread forks:** Choose **Fork from here** on a message or **Fork thread** in **Thread options** to branch into a new thread inheriting history up to that point; see [Forked Threads](configuration/threads.md#forked-threads).
- **Thread organization:** From **Thread options**, use **Edit tags**, **Resolve** or **Reopen thread** (`Cmd+Shift+Enter` / `Ctrl+Shift+Enter`), **Snooze** or **Unsnooze**, **Pin thread**, and **Edit summary** or **Regenerate summary**; see [Thread Exports](thread-exports.md) for exporting thread history.
- **Streaming and stopping:** Active replies stream live with reasoning blocks, tool call activity, and a stop button to cancel mid-turn; see [Streaming Responses](streaming.md) and [Stop Button](chat-commands.md#stop-button).
- **Reply details:** Open **Reply details** on an agent reply to inspect the model, token counts (input, cached, output), context window usage, time to first token, cost for that reply and the conversation so far, and technical IDs; see [Usage Tracking](usage.md).
- **Interactive questions:** When an agent asks a multiple-choice question, click an emoji reaction or type the option number to answer; see [Interactive Q&A](interactive.md).
- **Tool approvals:** Pending tool calls appear as approval cards with **Approve** and **Deny** actions (eligible cards offer **Approve once** alongside timed auto-approval durations); see [Tool Approval](tool-approval.md).
- **Bug reports:** When bug report admins are configured, **Report a bug** in a message's menu opens a consent dialog and leaves encrypted-room message text out unless you opt in to **Include message text**; without configured admins, **Download bug report** (shown when **Developer Tools** is enabled) saves the full bundle locally without that dialog or redaction — see [Bug Reports](bug-reports.md).

## Keyboard Shortcuts

Press `Cmd+/` (`Ctrl+/` on Linux and Windows) to open the **Keyboard shortcuts** dialog when no other dialog or focused Computer view is intercepting keys.
On Apple devices, `Cmd` is `⌘` and `Option` is `⌥`.

| Action | Apple (macOS / iOS / iPadOS) | Linux / Windows / Android |
| --- | --- | --- |
| Search and commands (command palette) | `Cmd+K` | `Ctrl+K` |
| Search messages (palette Messages tab) | `Cmd+Shift+F` | `Ctrl+Shift+F` |
| Open settings | `Cmd+,` | `Ctrl+,` |
| Show keyboard shortcuts | `Cmd+/` | `Ctrl+/` |
| Show or hide the room list | `Cmd+\` | `Ctrl+\` |
| Show or hide Members panel | `Cmd+Shift+M` | `Ctrl+Shift+M` |
| Go to the call | `Cmd+Shift+L` | `Ctrl+Shift+L` |
| Previous / next room | `Option+Up` / `Option+Down` | `Alt+Up` / `Alt+Down` |
| Previous / next unread room | `Option+Shift+Up` / `Option+Shift+Down` | `Alt+Shift+Up` / `Alt+Shift+Down` |
| Focus previous / next thread | `Cmd+Option+Up` / `Cmd+Option+Down` | `Ctrl+Alt+Up` / `Ctrl+Alt+Down` |
| Enter / leave thread | `Option+Right` / `Option+Left` | `Alt+Right` / `Alt+Left` |
| Resolve or reopen thread | `Cmd+Shift+Enter` | `Ctrl+Shift+Enter` |
| Mark as read and go to composer | `Esc` | `Esc` |
| Send / new line (desktop) | `Enter` / `Shift+Enter` | `Enter` / `Shift+Enter` |
| Send / new line (phone / tablet hardware keyboard) | `Cmd+Enter` / `Enter` | `Ctrl+Enter` / `Enter` |
| Edit your last message (empty composer) | `Up` | `Up` |
| Bold / italic / underline | `Cmd+B` / `Cmd+I` / `Cmd+U` | `Ctrl+B` / `Ctrl+I` / `Ctrl+U` |
| Strikethrough / inline code / spoiler | `Cmd+Shift+X` / `Cmd+E` / `Cmd+Shift+H` | `Ctrl+Shift+X` / `Ctrl+E` / `Ctrl+Shift+H` |
