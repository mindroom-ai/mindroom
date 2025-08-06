# MindRoom Configuration Widget - Visual Preview

## Widget Interface Overview

The widget provides a clean, intuitive interface for managing MindRoom agents:

```
┌─────────────────────────────────────────────────────────────────┐
│ MindRoom Agent Configuration                          [Sync ✓]   │
├─────────────────────────────────────────────────────────────────┤
│ [Agents] [Models & API Keys]                                     │
├─────────────────────────────────────────────────────────────────┤
│ ┌─────────────────┐ ┌─────────────────────────────────────────┐ │
│ │   Agents        │ │          Agent Details                  │ │
│ │                 │ │                                         │ │
│ │ 🤖 GeneralAgent │ │ Display Name: GeneralAgent              │ │
│ │    0 tools • 2  │ │                                         │ │
│ │                 │ │ Role: A general-purpose assistant that  │ │
│ │ 🤖 Calculator   │ │ provides helpful, conversational        │ │
│ │    1 tools • 3  │ │ responses to users.                     │ │
│ │                 │ │                                         │ │
│ │ 🤖 CodeAgent    │ │ Model: [default ▼]                      │ │
│ │    2 tools • 3  │ │                                         │ │
│ │                 │ │ Tools:                                  │ │
│ │ 🤖 ResearchAgent│ │ □ calculator  □ file      □ shell       │ │
│ │    3 tools • 4  │ │ □ python      □ csv       □ pandas      │ │
│ │                 │ │ □ yfinance    □ arxiv     □ duckduckgo  │ │
│ │                 │ │ □ wikipedia   □ newspaper □ website     │ │
│ │ [+ Add Agent]   │ │                                         │ │
│ └─────────────────┘ │ Instructions:                           │ │
│                     │ • Always provide a clear, helpful       │ │
│                     │   response to the user                  │ │
│                     │ • Remember context from conversation    │ │
│                     │ • Be conversational and friendly        │ │
│                     │ • Ask clarifying questions when needed  │ │
│                     │ [+ Add instruction]                     │ │
│                     │                                         │ │
│                     │ Rooms: ☑ lobby ☑ help □ dev □ research │ │
│                     │                                         │ │
│                     │ History Runs: [5]                       │ │
│                     │                                         │ │
│                     │ [💾 Save] [🗑️ Delete]                   │ │
│                     └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## Key Features Visible in the UI

### 1. Agent List (Left Panel)
- Shows all configured agents
- Displays agent icon, name, and stats
- Quick overview of tools and rooms
- Add new agent button

### 2. Agent Editor (Right Panel)
- Edit all agent properties
- Checkbox grid for tool selection
- Dynamic instruction management
- Room assignment with checkboxes
- Model selection dropdown
- Save and delete buttons

### 3. Models & API Keys Tab
```
┌─────────────────────────────────────────────────────────────────┐
│ Model Configuration                        [💾 Save All Changes] │
├─────────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ default                                    [🧪 Test] [✏️ Edit] │ │
│ │ Provider: ollama                                             │ │
│ │ Model: devstral:24b                                          │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ anthropic                                  [🧪 Test] [✏️ Edit] │ │
│ │ Provider: anthropic                                          │ │
│ │ Model: claude-3-5-haiku-latest                               │ │
│ │ API Key: ******************************* [👁️] [Change]      │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│ [+ Add New Model]                                                │
└─────────────────────────────────────────────────────────────────┘
```

### 4. Status Indicators
- **Sync Status**: Shows real-time sync state (✓ Synced, 🔄 Syncing, ⚠️ Error)
- **Connection Tests**: Test model connections with visual feedback
- **Save Confirmation**: Toast notifications for successful operations

## User Experience Flow

1. **Select Agent**: Click on an agent in the left panel
2. **Edit Properties**: Modify any field in the right panel
3. **Real-time Updates**: Changes are tracked with "dirty" state
4. **Save Changes**: Click Save to persist to config.yaml
5. **Automatic Sync**: File changes are detected and UI updates

## Design Principles

- **Clean & Modern**: Using Tailwind CSS for consistent styling
- **Intuitive**: Familiar patterns (checkboxes, dropdowns, buttons)
- **Responsive**: Adapts to different screen sizes
- **Accessible**: Proper labels and keyboard navigation
- **Feedback**: Clear status indicators and notifications

## Technical Integration

The widget seamlessly integrates with MindRoom:
- Reads from `config.yaml` on load
- Saves changes back to `config.yaml`
- Detects external file changes
- Works alongside manual YAML editing
- No data loss or conflicts

This proof-of-concept demonstrates how a visual configuration interface can make MindRoom more accessible while maintaining full compatibility with the existing YAML-based configuration system.
