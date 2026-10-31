# Config and CLI: one owner per validation rule, load failure, runtime path, and preset

This record holds the decisions behind the owners of the tool-entry lazy-flag rule, of a failed config load, of the CLI's runtime resolution and provider presets, and of the checks `mindroom doctor` shares with `mindroom config validate`.

## Decisions

- The "no `defer` or `initial` on presets and control-plane tools" rule belongs to `ToolConfigEntry`, because it needs nothing beyond one entry.
  It is checked first in the entry's lazy-flag validator, so the documented preset message still wins over "initial requires defer" without a second parser of the raw YAML forms.
  The error points at the entry (`agents → code → tools → 0`), and for a malformed entry its field parsing and entry-shape errors come first.
  `DefaultsConfig` keeps rejecting every flag in `defaults.tools`; that is a different rule.
- A failed load is recorded only as `ConfigLoad.error`; the API derives the 422 detail or the fixed 500 text from it when a request reads the config, so `config/` never chooses HTTP status codes.
  `uses_includes` stays a separate field because a failed load still knows it and no exception carries it after a successful parse.
- `config show`, `config path`, and `config edit` only need the file path, so they find it without resolving a runtime and never read the `.env` beside it; an unreadable `.env` must not block inspecting or repairing the config.
  Every other command resolves its runtime through `activate_cli_runtime`, except the installed-service status check, which resolves from the environment saved in the service unit, not the caller's.
- `config migrate` loads through the normal quiet loader and compares the file bytes before and after.
  An edit made by someone else between the command's read and the loader's read can make it report "Applied migration" for that edit; the migration's own compare-and-replace still uses the loader's bytes, so no write is lost.
- Each provider preset's facts live in one table row: menu label and text, accepted aliases, the spelling help shows, and its setup hints.
  The hints stay the exact sentence for each place (first-run setup, `config init` for hosted and self-hosted Matrix) instead of one phrase with generated suffixes, because the sentences place clauses differently (Azure's `.env` values, then its deployment name in `config.yaml`, then a restart).
  A row without setup hints is a preset that needs exactly one API key, which first-run setup asks for; Azure takes a key but has setup hints, so it never gets that prompt.
- `mindroom doctor` runs `config validate`'s two checks beyond loading: an automation no loaded plugin provides fails the config step, and every environment variable a model reads but nothing sets is one provider warning.
  That shared warning replaces doctor's own "key not set" line, so one missing variable is reported once.
  Provider probes keep their own preconditions: a Vertex AI model missing its project gets both the shared warning and the probe's "could not validate connection", and an Ollama model without a host gets the shared `OLLAMA_HOST` warning even when the SDK default server answers, as `config validate` warns today.

## Rejected alternatives

- Making the dashboard `reference="model"` hint the only declaration of model fields, checked by one walker: a hint names a target type, not when a reference must resolve (a disabled knowledge graph, sandbox runner configs, plugin-check configs).
  A schema test requires the hint on every model-named field that is not a provider's own model ID.
- Gating doctor's provider probes on the shared environment warnings: that needs per-route policy, since Ollama must keep probing its SDK default host.
- Writing the config bundle's journal and metadata through `durable_write`: its directory fsync can raise after the replacement, which changes the recovery outcomes the bundle installer reports.
- Composing setup hints from one phrase per preset: see the hint decision above.
