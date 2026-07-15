---
name: mindroom-docs
description: MindRoom documentation corpus for accurate product, configuration, and workflow guidance.
metadata: '{openclaw:{always:true}}'
---

# MindRoom Docs

Use this skill when the user asks how MindRoom works, how to configure it, or which commands/workflows to follow.

## Inputs

- Use the current conversation request.

## Workflow

1. Load `reference-index.md` first to discover the best page files.
2. Load the smallest number of page references needed with `get_skill_reference(...)`.
3. Use `llms.txt` for high-level navigation only.
4. Use `llms-full.txt` only when the answer spans many sections and page-level references are insufficient.
5. For setup or administration requests, inspect the available capabilities before claiming the change cannot be performed. Discover and use `config_manager` for changes it supports, then use the documented config-file workflow for other requested changes.
6. For dashboard questions, load the dashboard page reference and distinguish the MindRoom dashboard from Matrix clients such as Cinny or Element.
7. Answer with concrete steps and include the exact reference filenames used.

## Rules

- Prefer page-level references (`page__*.md`) over `llms-full.txt`.
- Do not invent behavior not present in the references.
- Inspect live configuration when the answer depends on current state; do not infer it from generic documentation.
- If information is missing, state that it is not documented in this skill corpus.

## Available references

- `reference-index.md`: mapping of docs pages to reference files.
- `llms.txt`: compact docs index.
- `llms-full.txt`: full combined docs corpus.
- `page__*.md`: per-page rendered markdown references.
