---
name: agent-conversations
description: Use whenever the current message comes from another agent, and whenever a request involves another agent or team in the room, such as asking, consulting, informing, handing work to, or debating with one, or you are about to mention or name one.
---

# Agent conversations

An agent takes a turn only when a message wakes it.
Your reply is posted in this thread for everyone to read, but it wakes only the agents it mentions.
Call `matrix_message` only to wake an agent, so every call you make has a `recipient`; everything else you say goes in your reply.

## What wakes an agent

| You do | Effect |
| --- | --- |
| `matrix_message(recipient="critic", message="...")`, with an agent `name` from `matrix_room(action="agents")` | That agent takes a turn in this thread; the call returns at once, without its answer |
| `matrix_message` without `recipient` | Posts a message that wakes no one, even if it contains `@names` |
| `@critic` or the agent's Matrix ID in your reply | That agent takes a turn after your reply finishes |
| Two or more agents mentioned in one reply, counting yourself | They answer together as one team reply |
| A display name such as "the Devil's Advocate", without `@` | Nobody wakes |

A message from an agent wakes you only if it mentions you or names you as `recipient`.

## Asking an agent

1. Call `matrix_message(recipient="<name>", message="<request>")` once.
   Write a self-contained request and end it with one of these sentences, filling in your own `name`:
   - you need the answer to finish: "When done, start your reply with @<your name>."
   - the person should get the answer: "Reply with the answer for the person and mention no agent."
   - you only inform it, even when the person says it need not reply: "Reply with one short line and mention no agent."
     Every agent you wake must reply, so never tell it not to.
2. End your turn with your reply: one or two sentences telling the person what you asked, mentioning no agent.
   The answer wakes you in a new turn, so do not read the thread to wait for it.
3. When the answer wakes you, continue the task.
   If your earlier reply already used that answer, reply in one short line that mentions no agent.

## When an agent asks you for something

1. Do what the request asks yourself; involve another agent only when the request tells you to.
2. Your reply is your answer and the only message you post; do not call `matrix_message` to deliver it.
3. Follow the request's last sentence: start your reply with the mention it asks for, or mention no agent.

## Naming an agent without waking it

Write its display name without `@`, such as "the Data Analyst", never its `@name` or Matrix ID.

## Coordinating several agents

When the person asks you to involve several agents, you run the conversation and every agent replies to you.

1. Ask one agent at a time, in the order the person wants.
2. Each time an answer wakes you, ask the next agent and tell the person the progress in one line; count turns when the person asked for several rounds.
3. When the person wants independent answers, ask every agent at once, one `matrix_message` each; when an answer wakes you before the others, reply in one line naming who you still wait for.
4. When every requested contribution is in the thread, give the person the final answer, naming each contributor by display name.

## Ending

Your final answer to the person mentions no agent, yourself included.
Never thank or acknowledge an agent with a mention, because that starts another turn.
