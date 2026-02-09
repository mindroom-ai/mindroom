---
name: mindroom-troubleshoot
description: Diagnose and resolve common MindRoom issues including agent failures, Matrix connectivity, model API errors, and deployment problems
user-invocable: true
---

# MindRoom Troubleshooting Guide

You are a MindRoom diagnostics assistant. Use this guide to help users diagnose and resolve issues with their MindRoom installation. Work through the decision tree systematically: identify symptoms, determine likely causes, and suggest targeted solutions.

When the user describes a problem, match it to the closest category below and walk through the diagnostic steps. If running a healthcheck is appropriate, suggest running the healthcheck script from the project root: `bash skills/mindroom-troubleshoot/scripts/healthcheck.sh`.

---

## 1. Agent Not Responding to Messages

### Symptoms
- User mentions an agent with `@mindroom_<name>` but gets no reply
- Agent appears online but ignores messages
- Thread never receives a response after mentioning an agent

### Likely Causes

**A. Agent not in the room**
- Agent was removed from the room config, or room IDs changed after a Matrix state reset.
- Check: Look at `config.yaml` under the agent's `rooms:` list. Verify the room alias matches what exists on the homeserver.
- Fix: Add the correct room alias to the agent's `rooms:` list in `config.yaml`. Hot-reload will pick it up.

**B. Authorization failure**
- The sender is not in the `authorization.global_users` list or a room-specific allowlist.
- Check: Look at `config.yaml` under `authorization:`. Verify the sender's full Matrix user ID (e.g., `@user:localhost`) is listed.
- Fix: Add the user's Matrix ID to `authorization.global_users` or the room-specific list.

**C. Agent failed to start**
- The agent's sync loop crashed. Look for `"Failed to start agent"` or `"Sync loop failed"` in logs.
- Check: Run with `--log-level DEBUG` and look for errors during startup. Common causes: invalid model ID, missing API key, Matrix auth failure (M_FORBIDDEN).
- Fix: Correct the underlying issue (API key, model config) and restart. The orchestrator retries with exponential backoff (up to 3 attempts).

**D. Response tracker thinks it already replied**
- The `ResponseTracker` (stored in `mindroom_data/tracking/`) may have marked the event as already handled.
- Check: This happens after restarts when old events are replayed. The tracker prevents duplicates.
- Fix: If stuck, remove the tracking file for the agent: `rm mindroom_data/tracking/<agent_name>_responded.json` and restart.

**E. Router not routing**
- In multi-agent rooms, the router agent performs AI-based routing. If the router is down, no agent gets triggered.
- Check: Look for the router agent in logs. It should log `"Starting sync loop for router"`.
- Fix: Ensure the router is configured and has a valid model. The router uses the model specified under `router.model` in `config.yaml`.

**F. Message is an edit**
- Agents can regenerate responses when a message is edited, but only if the agent previously responded to the original message. The edit-regeneration flow is in `bot.py:_handle_message_edit()`.
- Check: Verify the user sent a new message. If it was an edit, check whether the agent had previously responded to the original — if not, the agent will not respond to the edit.

### Diagnostic Steps
1. Check logs for `"Received message"` entries for the target room -- if absent, the agent is not syncing that room.
2. Check logs for `"Authorization check"` -- if `authorized=False`, it is an auth issue.
3. Check logs for `"Will respond"` (e.g., `"Will respond: only agent in thread"`) -- if absent, routing or mention logic filtered it out.
4. Check if the agent's sync task is alive: look for `"Starting sync loop"` or `"Sync loop failed"` log entries.

---

## 2. Matrix Connection Failures

### Symptoms
- `Failed to login`: agent cannot authenticate with the homeserver
- `ConnectionRefusedError` or `ClientConnectorError` on startup
- SSL certificate verification errors
- `M_FORBIDDEN`, `M_UNKNOWN_TOKEN`, or `M_USER_DEACTIVATED` errors

### Likely Causes

**A. Homeserver unreachable**
- The homeserver URL in `MATRIX_HOMESERVER` env var is wrong or the server is down.
- Check: `curl -s $MATRIX_HOMESERVER/_matrix/client/versions` -- should return JSON with supported versions. If `MATRIX_SSL_VERIFY=false`, add `-k`: `curl -sk $MATRIX_HOMESERVER/_matrix/client/versions`.
- Fix: Correct the URL. For local dev: `http://localhost:8008`. For production: use the actual homeserver URL.

**B. SSL verification failure**
- Self-signed certificates or dev environments without proper TLS.
- Check: Error mentions `SSLCertVerificationError` or `CERTIFICATE_VERIFY_FAILED`.
- Fix: Set `MATRIX_SSL_VERIFY=false` for development. In production, use proper certificates.

**C. Agent credentials invalid**
- Credentials in `mindroom_data/matrix_state.yaml` are stale or the homeserver was reset.
- Check: Look for `M_FORBIDDEN` or `M_UNKNOWN_TOKEN` in logs during login.
- Fix: Delete `mindroom_data/matrix_state.yaml` and restart. The orchestrator will re-register all agents.

**D. Registration disabled on homeserver**
- Synapse may have registration disabled, preventing new agent accounts.
- Check: Look for `M_FORBIDDEN` during registration (not login).
- Fix: Enable registration in Synapse config (`enable_registration: true`) or use the Synapse admin API to create accounts.

**E. Stale Matrix state after homeserver change**
- Switching between homeservers (e.g., local to remote) without clearing state.
- Check: Room IDs in `mindroom_data/matrix_state.yaml` don't match the current homeserver.
- Fix: `rm -f mindroom_data/matrix_state.yaml` and restart.

### Diagnostic Steps
1. Test homeserver: `curl -s $MATRIX_HOMESERVER/_matrix/client/versions` (add `-k` if `MATRIX_SSL_VERIFY=false`)
2. Check env vars: `echo $MATRIX_HOMESERVER` -- should be set correctly.
3. Test login manually: use `matty rooms` to see if the client can authenticate.
4. Check `mindroom_data/matrix_state.yaml` for stale entries.

---

## 3. Model API Errors

### Symptoms
- Agent responds with `Authentication failed (openai)` or `Authentication failed (anthropic)`
- `Rate limited. Please wait a moment and try again.`
- `Request timed out. Please try again.`
- Agent responds with generic `Error: ...` messages

### Likely Causes

**A. Missing or invalid API key**
- The API key for the provider is not set or is incorrect.
- Check: Look at `.env` for the relevant key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc.). Also check `mindroom_data/credentials/` for UI-set keys.
- Fix: Set the correct API key in `.env` or via the MindRoom dashboard credentials page.

**B. Wrong model ID**
- The model name in `config.yaml` under `models:` does not match what the provider offers.
- Check: Look at the `models:` section. Common mistake: using `claude-3` instead of `claude-sonnet-4-5-latest`, or a model that has been deprecated.
- Fix: Update the model `id` to a valid model name from the provider.

**C. Rate limiting (429)**
- Too many requests to the provider API.
- Check: Error message contains "rate" or "429" or "quota".
- Fix: Wait and retry. Consider using a different model or provider for less critical agents. Check your plan limits with the provider.

**D. Timeout**
- The model API is slow or the request is too large.
- Check: Error message contains "timeout".
- Fix: Reduce context size, use a faster model, or increase timeout settings if available.

**E. Base URL misconfigured**
- For self-hosted models (Ollama, llama.cpp, vLLM), the base URL is wrong.
- Check: Look at `models:` in `config.yaml` for `extra_kwargs.base_url`. For Ollama, also check `OLLAMA_HOST`.
- Fix: Ensure the base URL points to the running inference server and the model ID matches what is loaded.

### Diagnostic Steps
1. Verify API key is set: check `.env` and `mindroom_data/credentials/`.
2. Test the API directly: `curl` the provider's API with your key.
3. Verify model ID: check the provider's documentation for valid model names.
4. For local models: `curl -s http://localhost:9292/v1/models` (or the configured base URL).

---

## 4. Tool Execution Failures

### Symptoms
- Agent says `Tool '<name>' failed: ...` or `Tool '<name>' not found`
- Agent acknowledges the request but the tool action does not happen
- `Failed to load tool for skill dispatch` in logs

### Likely Causes

**A. Tool not configured for agent**
- The tool name is not listed in the agent's `tools:` list in `config.yaml`.
- Check: Look at the agent's config. Tool names must match registered tool names exactly.
- Fix: Add the tool name to the agent's `tools:` list.

**B. Tool requires external service**
- Some tools need external services (e.g., Home Assistant, Google Calendar) with API keys or URLs.
- Check: Look at the tool's error message for what is missing.
- Fix: Configure the required integration via the dashboard or `.env`.

**C. Tool function signature mismatch**
- The tool's function signature changed or the agent is calling it with wrong arguments.
- Check: Look for `"Tool requires parameters"` in the error message.
- Fix: This is typically a code bug. Check `src/mindroom/tools/` for the tool implementation.

### Diagnostic Steps
1. Check agent config: verify `tools:` list includes the desired tool.
2. Check logs for `"Failed to load tool"` during startup.
3. Try the tool with a different agent to isolate the issue.

---

## 5. Memory / Knowledge Base Not Working

### Symptoms
- Agent does not remember previous conversations
- Knowledge base queries return no results
- `Unsupported knowledge embedder provider` error
- Memory search returns empty results

### Likely Causes

**A. Embedder not configured**
- Memory and knowledge bases require an embedding model. Default is OpenAI `text-embedding-3-small`.
- Check: Look at `config.yaml` under `memory.embedder`. Verify the provider and model are set.
- Fix: Configure the embedder. For local: use Ollama with an embedding model. For cloud: ensure the API key is set.

**B. Ollama embedder not running**
- When using Ollama for embeddings, the Ollama server must be running with the embedding model loaded.
- Check: `curl -s http://localhost:11434/api/tags` -- verify the embedding model is listed.
- Fix: Pull the model: `ollama pull <model>`. Ensure Ollama is running.

**C. Knowledge base path does not exist**
- The `path` in `knowledge_bases:` config points to a non-existent directory.
- Check: Verify the path exists and contains supported files (PDF, TXT, MD, etc.).
- Fix: Create the directory and add files, or correct the path.

**D. Knowledge base not assigned to agent**
- The knowledge base exists but is not linked to the agent via `knowledge_base:` in the agent config.
- Check: Look at the agent's config for `knowledge_base: <base_id>`.
- Fix: Add `knowledge_base: <base_id>` to the agent's config.

**E. ChromaDB storage issues**
- The vector database storage under `mindroom_data/` may be corrupted or have permission issues.
- Check: Look for ChromaDB errors in logs. Check permissions on `mindroom_data/`.
- Fix: Delete the ChromaDB collection directory and let it re-index.

### Diagnostic Steps
1. Check `config.yaml` for `memory.embedder` configuration.
2. Verify the embedding provider is accessible (API key or local service).
3. Check that knowledge base paths exist and contain files.
4. Look for indexing errors in logs at startup.

---

## 6. Hot-Reload Not Picking Up Config Changes

### Symptoms
- Changed `config.yaml` but agents do not update
- New agents do not appear after adding them to config
- Removed agents still respond

### Likely Causes

**A. File watcher not detecting changes**
- The file watcher polls `config.yaml` every second by checking `st_mtime`. Some editors (vim with backup files, some IDEs) may write to a temp file and rename, which can be missed briefly.
- Check: Look for `"Configuration file changed"` in logs after saving.
- Fix: Save the file again. If using vim, try `:w!`. The watcher will pick it up on the next poll cycle.

**B. YAML parse error in new config**
- If the new `config.yaml` has syntax errors, `Config.from_yaml()` will fail and the old config remains active.
- Check: Look for YAML parsing errors in logs. Validate: `python -c "import yaml; yaml.safe_load(open('config.yaml'))"`.
- Fix: Fix the YAML syntax error.

**C. Config path override**
- `MINDROOM_CONFIG_PATH` or `CONFIG_PATH` env var may point to a different file than what you are editing. Note: the file watcher watches the actual config path passed at startup, so if you override the path via env var, changes to the overridden file should be detected. However, if you're editing a different file than the one MindRoom loaded, changes won't take effect.
- Check: `echo $MINDROOM_CONFIG_PATH $CONFIG_PATH` -- if set, MindRoom reads from that path.
- Fix: Edit the correct file, or unset the env var. If hot-reload does not work with an overridden path, restart the process.

**D. Agent start failure during reload**
- The new agent may fail to start (bad model ID, missing API key), and the orchestrator logs the failure but continues.
- Check: Look for `"Failed to start agent"` in logs after the config change.
- Fix: Fix the agent configuration and save again.

### Diagnostic Steps
1. Check logs for `"Configuration file changed"` after saving.
2. Check logs for `"Configuration update applied"` or `"No agent changes detected"`.
3. Validate YAML: `python -c "import yaml; yaml.safe_load(open('config.yaml'))"`.
4. Check which config file is being read: look for `"Loading config from"` in startup logs.

---

## 7. Room Creation / Joining Issues

### Symptoms
- Rooms not appearing in Matrix client
- `Failed to create room` errors
- Agents not joining their configured rooms
- `matty rooms` shows no rooms

### Likely Causes

**A. Router agent not running**
- The router agent creates rooms on behalf of all agents. If it fails to start, no rooms are created.
- Check: Look for `"Router not available, cannot ensure rooms exist"` in logs.
- Fix: Fix the router's configuration (model, API key) and restart.

**B. Room alias collision**
- A room with the same alias already exists on the homeserver but is not in MindRoom's state.
- Check: The room will be found by alias resolution and joined instead of created. This is normal behavior.
- Fix: If you want a fresh room, delete the old one from the homeserver first.

**C. Stale room state**
- `mindroom_data/matrix_state.yaml` has room IDs that no longer exist on the homeserver.
- Check: Look for `"Removing stale room"` in logs.
- Fix: Delete `mindroom_data/matrix_state.yaml` and restart. Rooms will be recreated.

**D. Agent not invited to room**
- The agent exists but was not invited to join the room.
- Check: The orchestrator should handle invitations via `_ensure_room_invitations()`. Look for invitation errors in logs.
- Fix: Restart to trigger the invitation flow, or manually invite the agent from your Matrix client.

### Diagnostic Steps
1. Check logs for room creation: `"Created room"` or `"Failed to create room"`.
2. Check `mindroom_data/matrix_state.yaml` for room entries.
3. Use `matty rooms` to see what rooms exist.
4. Verify room aliases with: `curl -s "$MATRIX_HOMESERVER/_matrix/client/v3/directory/room/%23<alias>:<server>"`

---

## 8. Scheduling Not Triggering

### Symptoms
- Scheduled tasks do not fire at the expected time
- `!schedule` command works but nothing happens at the scheduled time
- Tasks are lost after restart

### Likely Causes

**A. Timezone mismatch**
- Schedules are evaluated in the timezone configured in `config.yaml` under `timezone:`.
- Check: Look at the `timezone` setting. Default may not match your local time.
- Fix: Set `timezone: America/New_York` (or your timezone) in `config.yaml`.

**B. Router agent restarted**
- Only the router agent restores scheduled tasks on startup. If the router fails to start, schedules are not restored.
- Check: Look for `"Restored N scheduled tasks"` in logs after startup.
- Fix: Ensure the router starts successfully.

**C. Cron expression error**
- The AI-generated cron expression may not match user intent.
- Check: Use `!schedule list` to see active schedules and their cron expressions.
- Fix: Cancel and recreate the schedule with more explicit timing.

**D. Agent mentioned in schedule is not available**
- The scheduled message mentions an agent that is no longer configured or not in the room.
- Check: Look at the scheduled message content and verify the mentioned agents exist.
- Fix: Update the schedule to mention available agents.

### Diagnostic Steps
1. List active schedules: `!schedule list` in the room.
2. Check timezone: look at `timezone:` in `config.yaml`.
3. Check logs for `"Restored scheduled tasks"` after startup.
4. Verify cron expressions: `python -c "from croniter import croniter; print(croniter('CRON_EXPR').get_next())"`.

---

## 9. Docker / Deployment Issues

### Symptoms
- Container starts but agents fail to connect
- `OSError: Device or resource busy` on file writes
- Persistent data lost between container restarts
- Health endpoint not responding

### Likely Causes

**A. Storage path not mounted**
- `mindroom_data/` must be on a persistent volume in Docker/Kubernetes.
- Check: `ls mindroom_data/` inside the container -- if empty after restart, it is not persisted.
- Fix: Mount a volume at the `STORAGE_PATH` location (default: `mindroom_data/`).

**B. Bind mount atomic rename failure**
- Docker bind mounts may fail on `Path.replace()` with `OSError: [Errno 16]`. MindRoom has a fallback (`safe_replace` in `constants.py`) that uses `shutil.copy2` instead.
- Check: Look for `"Device or resource busy"` in logs.
- Fix: This is handled automatically. If it persists, use a named Docker volume instead of a bind mount.

**C. Config file not mounted**
- `config.yaml` must be accessible inside the container.
- Check: `ls /path/to/config.yaml` inside the container. Check `CONFIG_PATH` or `MINDROOM_CONFIG_PATH` env var.
- Fix: Mount the config file or use the `MINDROOM_CONFIG_TEMPLATE` mechanism to seed from a template.

**D. Environment variables not set**
- API keys and homeserver URL must be passed as env vars or secret files.
- Check: Required env vars: `MATRIX_HOMESERVER`, at least one provider API key. Use `_FILE` suffix for secrets from files (e.g., `OPENAI_API_KEY_FILE`).
- Fix: Set env vars in your Docker Compose, Kubernetes secrets, or `.env` file.

**E. Health endpoint not responding**
- The API server runs on port 8765 by default.
- Check: `curl -s http://localhost:8765/api/health` -- should return `{"status": "healthy"}`.
- Fix: Verify the API server is running. Check port mapping in Docker. The API server is separate from the bot process.

**F. Network isolation**
- Container cannot reach the Matrix homeserver or model API.
- Check: From inside the container: `curl -s $MATRIX_HOMESERVER/_matrix/client/versions` (add `-k` if `MATRIX_SSL_VERIFY=false`).
- Fix: Ensure proper Docker networking. Use `host.docker.internal` for host services on Docker Desktop, or use Docker network aliases.

### Diagnostic Steps
1. Check container logs: `docker logs <container>` or `kubectl logs <pod>`.
2. Verify mounts: check that `mindroom_data/` and `config.yaml` are properly mounted.
3. Test connectivity: `curl` the homeserver and model API from inside the container.
4. Check health: `curl -s http://localhost:8765/api/health`.
5. Run the bundled healthcheck from the project root: `bash skills/mindroom-troubleshoot/scripts/healthcheck.sh`.

---

## Quick Reference: Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `MATRIX_HOMESERVER` | Matrix homeserver URL | `http://localhost:8008` |
| `MATRIX_SSL_VERIFY` | Enable/disable SSL verification | `true` |
| `MATRIX_SERVER_NAME` | Server name for federation | derived from homeserver URL |
| `STORAGE_PATH` | Base directory for persistent data | `mindroom_data` |
| `MINDROOM_CONFIG_PATH` | Override config file location | `config.yaml` in project root |
| `MINDROOM_ENABLE_STREAMING` | Enable streaming responses | `true` |
| `OPENAI_API_KEY` | OpenAI API key | (none) |
| `ANTHROPIC_API_KEY` | Anthropic API key | (none) |
| `GOOGLE_API_KEY` | Google/Gemini API key | (none) |
| `OPENROUTER_API_KEY` | OpenRouter API key | (none) |
| `DEEPSEEK_API_KEY` | DeepSeek API key | (none) |
| `CEREBRAS_API_KEY` | Cerebras API key | (none) |
| `GROQ_API_KEY` | Groq API key | (none) |
| `OLLAMA_HOST` | Ollama server URL | `http://localhost:11434` |

## Quick Reference: Key File Paths

| Path | Purpose |
|---|---|
| `config.yaml` | Main configuration file (agents, models, rooms, teams) |
| `mindroom_data/matrix_state.yaml` | Agent accounts (username/password) and room metadata (ID, alias, name, created_at) |
| `mindroom_data/tracking/` | Per-agent response tracking (prevents duplicate replies) |
| `mindroom_data/memory/` | Mem0 vector store for agent/room/team memories |
| `mindroom_data/credentials/` | API keys set via dashboard |
| `mindroom_data/sessions/` | Per-agent SQLite event history |
| `mindroom_data/encryption_keys/` | Matrix E2EE key storage |
| `.env` | Environment variables (API keys, homeserver URL) |
