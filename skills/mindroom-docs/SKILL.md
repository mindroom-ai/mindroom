---
name: mindroom-docs
description: MindRoom documentation reference - consult when users ask about configuration, tools, skills, memory, deployment, or any MindRoom feature
metadata:
  openclaw:
    always: true
---

# MindRoom Documentation

You have access to the full MindRoom documentation as reference material. Consult these references when users ask about:

- Getting started, installation, or first-time setup
- Configuring agents, models, teams, or the router
- Available tools and how to enable them
- Skills system and how to create or use skills
- Memory system (agent, room, and team scopes)
- Scheduling tasks with cron or natural language
- Voice message handling
- Authorization and access control
- Deployment (Docker, Kubernetes, Google OAuth)
- Architecture and Matrix integration
- CLI commands
- Dashboard usage
- Plugin development
- Knowledge bases

## Available references

- `llms.txt` - Index of all documentation pages with descriptions and links
- `llms-full.txt` - Complete documentation content inlined for deep reference

Reference paths are relative to this skill's `references/` directory.

Use `get_skill_reference("mindroom-docs", "llms.txt")` for a quick overview of available topics, then `get_skill_reference("mindroom-docs", "llms-full.txt")` when you need the full content of specific sections.
