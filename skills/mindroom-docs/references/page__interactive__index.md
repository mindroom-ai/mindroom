# Interactive Q&A

Agents can ask users a multiple-choice question that they answer by clicking an emoji reaction or typing the option number.
Use this when an agent needs the user to pick between a few concrete options before it continues.

<video controls playsinline preload="metadata" aria-label="The agent asks a multiple-choice question before it decides" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/14da1dfa-9892-4ce8-a2ed-ad67673ea4b5#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/9cef359e-15d0-4f0a-810a-ec34811cf561#t=0.1" type="video/mp4">
</video>

## Asking a Question

Any agent can ask a question without extra tools or configuration by including an `interactive` code block with JSON in its response:

````markdown
```interactive
{
    "question": "What approach would you prefer?",
    "options": [
        {"emoji": "🚀", "label": "Fast and automated", "value": "fast"},
        {"emoji": "🔍", "label": "Careful and manual", "value": "careful"}
    ]
}
```
````

MindRoom replaces the block with a numbered list and adds each option's emoji as a reaction button:

```
What approach would you prefer?

1. 🚀 Fast and automated
2. 🔍 Careful and manual

React with an emoji or type the number to respond.
```

While a response streams, the question appears once its code block is complete.

To make an agent use questions, describe the format in its `instructions` or `role`:

```yaml
agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant
    instructions:
      - >
        When the user needs to choose between options, present them using
        an interactive code block with JSON containing question and options
        (each with emoji, label, and value fields).
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | No | Question text shown above the options. Defaults to `"Please choose an option:"`. |
| `options` | array | Yes | Option objects; options beyond the fifth are dropped without warning. |
| `options[].emoji` | string | No | Reaction button for the option. Defaults to `"❓"`. |
| `options[].label` | string | No | Text shown for the option. Defaults to `"Option"`. |
| `options[].value` | string | No | Value passed back to the agent. Defaults to the label in lowercase. |

Give every option a different emoji; reacting with an emoji that several options share, including the default `❓`, selects the last of them, so the others can only be chosen by number.

## Answering a Question

Users answer where the question was asked, in its thread or in the main room, in either of two ways:

- **Reaction**: click one of the option emojis on the question.
- **Text**: send just the option number, a single digit from `1` to `5`.
  When several questions there are unanswered, a number answers the oldest question from each agent that asked one; react to answer a specific question.

Either way, the agent that asked receives the question text and the selected option's label and value, and continues the conversation there.
Each question accepts one answer, and questions stay answerable across restarts.
Only human users can answer; reactions and messages from agents are ignored.

## Limitations

- Only the first valid question in a response gets reaction buttons and numeric answers; later ones are shown as plain text.
- A very large question, roughly a few thousand characters of question text, option labels, and option values, is shown without reaction buttons and cannot be answered by number; shorten the question, labels, or values to keep it answerable.
- Questions only work in an agent's normal responses; sending or editing one with the `matrix_message` tool fails with `Interactive prompts are only supported in normal agent responses.`
