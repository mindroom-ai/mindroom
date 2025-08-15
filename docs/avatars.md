# Avatar System Documentation

## Overview

The MindRoom avatar system automatically generates and sets unique avatars for all agents and teams in the Matrix chat interface. Each avatar is generated using OpenAI's GPT Image model with a consistent visual style.

## Features

- **Automatic Generation**: Creates avatars for all configured agents and teams
- **Consistent Style**: Uses a unified Pixar-style friendly robot design language
- **Smart Regeneration**: Only generates missing avatars (idempotent operation)
- **Git LFS Integration**: Stores avatars efficiently using Git Large File Storage
- **Matrix Integration**: Automatically sets avatars during bot initialization

## Usage

### Generating Avatars

Run the avatar generation script:

```bash
# Using uv run (recommended)
uv run scripts/generate_avatars.py

# Or with Python directly
python scripts/generate_avatars.py
```

**Requirements:**
- `OPENAI_API_KEY` environment variable must be set (can be in `.env` file)
- OpenAI API access with GPT Image model permissions

### How It Works

1. **Generation Phase**:
   - Reads all agents and teams from `config.yaml`
   - Checks `avatars/agents/` and `avatars/teams/` directories
   - Generates avatars only for missing entities
   - Each avatar uses entity-specific visual themes

2. **Storage**:
   - Avatars are stored in `avatars/` directory
   - Git LFS tracks all image files automatically
   - Structure:
     ```
     avatars/
     ├── agents/
     │   ├── calculator.png
     │   ├── code.png
     │   ├── research.png
     │   └── ...
     └── teams/
         ├── code_team.png
         └── super_team.png
     ```

3. **Matrix Integration**:
   - During bot startup, each agent checks for its avatar
   - If found, uploads it to Matrix homeserver
   - Sets as profile picture (only if not already set)
   - Avatars persist across bot restarts

## Visual Themes

Each agent type has unique Pixar-style robot characteristics:

- **Calculator**: Calculator screen display, number pad buttons, mathematical hologram projections
- **Code**: Keyboard fingers, code-scrolling screen face, USB port accessories
- **Research**: Magnifying glass eye, book compartment, data scanner antenna
- **Finance**: Currency display screens, stock chart projectors, golden metallic finish
- **Security**: Shield attachments, lock mechanisms, protective armor plating
- **Router**: Network hub design, multiple connection ports, data stream effects
- **Teams**: Multiple robots together, interconnected designs, combined features from members

## Customization

To customize avatar generation:

1. Edit prompts in `scripts/generate_avatars.py`
2. Modify the `generate_prompt()` function
3. Adjust base style or agent-specific themes

## Troubleshooting

### Avatar Not Showing
- Check if avatar file exists in `avatars/` directory
- Verify bot has permission to upload media to Matrix
- Check logs for avatar upload errors

### Generation Failures
- Verify `OPENAI_API_KEY` is set correctly
- Check OpenAI API quota and rate limits
- Ensure network connectivity to OpenAI

### Git LFS Issues
- Run `git lfs install` if not initialized
- Check `.gitattributes` includes avatar patterns
- Verify Git LFS is tracking avatar files

## Manual Avatar Setting

To manually set an avatar for an agent:

1. Place image in appropriate directory:
   - Agents: `avatars/agents/<agent_name>.png`
   - Teams: `avatars/teams/<team_name>.png`

2. Restart the bot or wait for next initialization

The system will automatically detect and use the manual avatar.
