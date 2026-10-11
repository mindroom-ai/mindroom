---
name: open-knowledge-format
description: Use when answering from or editing an Open Knowledge Format (OKF) bundle, a knowledge folder of markdown concept files with YAML frontmatter and index.md and log.md files.
---

# Open Knowledge Format (OKF)

An OKF v0.2 bundle is a folder of markdown concept files.
Each concept starts with YAML frontmatter, and `type` is its only required field.
`index.md` files list a folder's concepts and `log.md` records changes; neither is a concept.
Links that start with `/` resolve from the bundle root, such as `/playbooks/deploy.md`; other links resolve from the file that contains them.

## Reading

1. Start at the bundle root `index.md` and follow indexes and links to the concepts you need.
2. Tell the user what the frontmatter says about each concept you rely on:

| Field | Meaning |
| --- | --- |
| `status: deprecated` | No longer current; find its replacement. |
| `status: draft` | Not reviewed; may be incomplete. |
| `stale_after` before now | Stale; ask a person to re-confirm. |
| no `verified` | Unverified. |
| `verified` only by non-`human:` actors | Machine-confirmed; not human-reviewed. |
| `verified` by a `human:` actor | Human-reviewed; name who and when. |
| `generated.at` after the newest `verified` time | Changed since it was last verified. |

3. Cite concept paths in your answer.
4. For `type: Attested Computation`, use its `# Computation` block, or the file its `computation` key names, exactly as written, and supply only values for its declared `parameters`.

## Writing

New concepts get this frontmatter shape; when you edit a concept, keep all its existing keys and values and change only what the edit requires:

```yaml
---
type: Playbook                    # required; keep the existing type when editing
title: "Payments: rotate the signing key"
description: "One sentence, reused in index.md."
tags: [payments, security]
generated: { by: "@analyst:example.org/gpt-6-luna", at: 2026-10-03T18:30:00-07:00 }
status: stable                    # stable, draft, or deprecated; no other values
sources:
  - id: ops-412
    resource: OPS-412             # required in every source: URL, bundle path, ticket, or "chat with @alice:example.org"
    title: "Ticket OPS-412: key rotation"
    author: human:@alice:example.org
---
```

- `generated` records your write, also on edits of concepts other people wrote: `by` is your Matrix ID, a slash, and your model; `at` is the current message's `ts` with its UTC offset, such as `2026-10-03T18:30:00-07:00` for `2026-10-03 18:30 PDT`.
  Keep existing `verified` entries when you edit; your newer `generated.at` already shows readers that the content changed after them.
- `verified` is a person's confirmation: append `{ by: "human:<their Matrix ID>", at: <current message ts with its UTC offset> }` to the `verified` list when a person tells you the content is correct, turning a single `{ by, at }` mapping into a list first.
  Recording a confirmation leaves `generated` as it is, because the content did not change.
- Retire a concept by setting `status: deprecated` and linking its replacement at the top of the body; keep the file.
- Write links to other concepts from the bundle root, such as `/services/billing-api.md`.
- Cite a source in the body with a footnote whose label is the source `id`, `...every 90 days.[^ops-412]`, and end the body with its definition, `[^ops-412]: Ticket OPS-412`.
- Put double quotes around every `title` and `description`, and around any other value that contains `: ` or starts with `@`, as in the template above.

After each write:

1. Update the folder's `index.md`: one line per concept, `* [Title](file.md) - description`, marking deprecated entries.
   A new folder also gets a line `* [Folder](folder/index.md) - description` in its parent's `index.md`.
2. Update the bundle root `log.md`: newest date first, one line per change under `## YYYY-MM-DD`, such as `* **Update**: Added a canary step to the [billing deploy playbook](/playbooks/deploy-billing.md).`
3. Read each changed concept back and check that its frontmatter still has every key it had before, each exactly once.
