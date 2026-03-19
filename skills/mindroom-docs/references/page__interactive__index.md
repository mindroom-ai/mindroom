# Interactive Q&A

MindRoom agents can present clickable multiple-choice questions to users using Matrix reactions. When an agent's response contains a specially formatted JSON block, MindRoom automatically renders it as a numbered list with emoji reactions that users can click to respond.

## How It Works

1. An agent includes an `interactive` code block in its response.
1. MindRoom parses the JSON, formats the options as a numbered list, and adds emoji reactions to the message.
1. The user clicks a reaction emoji or types the option number.
1. MindRoom captures the selection and feeds it back to the agent as a follow-up prompt (`"The user selected: <value>"`).

The entire flow happens within the thread where the original question was asked.

## JSON Format

Agents emit interactive questions by wrapping JSON in an `interactive` code block:

````
```interactive
{
    "question": "What approach would you prefer?",
    "options": [
        {"emoji": "🚀", "label": "Fast and automated", "value": "fast"},
        {"emoji": "🔍", "label": "Careful and manual", "value": "careful"}
    ]
}
````

```

### Fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `question` | string | No | The question text shown above options. Defaults to `"Please choose an option:"`. |
| `options` | array | Yes | List of option objects (max 5). |
| `options[].emoji` | string | No | Emoji shown as a reaction button. Defaults to `"❓"`. |
| `options[].label` | string | No | Human-readable label for the option. Defaults to `"Option"`. |
| `options[].value` | string | No | Value passed back to the agent when selected. Defaults to the label in lowercase. |

### Rendered Output

The JSON block is replaced with a formatted message:

```

What approach would you prefer?

1. 🚀 Fast and automated
1. 🔍 Careful and manual

React with an emoji or type the number to respond.

```

The corresponding emoji reactions are added to the message as clickable buttons.

## User Response Methods

Users can respond in two ways:

- **Reaction**: Click one of the emoji reactions added to the message.
- **Text**: Send a message with a single-digit option number (e.g., `1` or `2`) in the same thread. Only digits 1–5 are recognized; multi-digit numbers like `10` are ignored.

Both methods trigger the same follow-up behavior: the agent receives the selected value and continues the conversation.

## Agent Integration

Agents don't need any special tools or configuration to use interactive questions.
Any agent can include an `interactive` code block in its response text.
You can guide agents to use this feature through their `instructions` or `role`:

```

agents: assistant: display_name: Assistant role: A helpful assistant instructions: - > When the user needs to choose between options, present them using an interactive code block with JSON containing question and options (each with emoji, label, and value fields).

````

## Limitations

- Maximum of **5 options** per question. Additional options are silently truncated.
- Only **one active question per message**. If a response contains multiple interactive blocks, only the first is processed.
- Questions are tracked **in memory** and do not persist across restarts.
- Only human users can respond; reactions from other agents are ignored.
- Only the agent that created the question processes reactions to it.```
````
