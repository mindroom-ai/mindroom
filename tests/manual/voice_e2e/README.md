# Voice-call end-to-end harness

Runs real GPT-Live voice calls against a local MatrixRTC stack and a separate MindRoom instance, with a scripted synthetic caller.
Each call costs real GPT-Live minutes and delegate-model tokens.
The instance must include mindroom-ai/mindroom#2810, because delegated steps wait for its `call_delegation_stage` log events.

1. Create `~/.mindroom-voicetest/` (mode 700) with `config.yaml` (below), `.env` (provider keys, `MATRIX_HOMESERVER=http://localhost:8008`, `MATRIX_SERVER_NAME=localhost`, `SSL_CERT_FILE` pointing at a CA bundle so aiohttp can reach OpenAI), and `stack.env` (`LIVEKIT_NODE_IP=<LAN IP>`, `LIVEKIT_SECRET=<48 hex chars>`).
2. Create the Python 3.14 environment, which the `stall` scenario needs for `sys.remote_exec`: `UV_PROJECT_ENVIRONMENT=.venv314 uv sync --all-extras --python 3.14`.
3. Start the stack: `docker compose --env-file ~/.mindroom-voicetest/stack.env -f tests/manual/voice_e2e/compose.yaml up -d --wait`.
4. Start the instance: `tests/manual/voice_e2e/instance_ctl.sh start` (`MINDROOM_VOICETEST_REPO` selects another checkout).
5. Load the keys into your shell with `set -a; . ~/.mindroom-voicetest/.env; set +a`, then run a scenario: `.venv314/bin/python tests/manual/voice_e2e/voice_call_e2e.py baseline` (also `latency`, `probe`, `queue`, `stale_queue`, `stall`, `delegated_barge`).
6. Summarize: `python tests/manual/voice_e2e/analyze.py ~/.mindroom-voicetest/runs/<run>`.

The caller always calls the agent `openclaw` and reads call transcripts from its file-memory workspace, and the scenarios ask for calculator and web-search tools:

```yaml
models:
  default:
    provider: openai
    id: gpt-6-luna
agents:
  openclaw:
    display_name: OpenClaw
    memory_backend: file
    tools: [calculator, duckduckgo]
    access:
      users: ["@voicecaller:localhost"]
calls:
  enabled: true
  livekit_service_url: http://127.0.0.1:8781
  profiles:
    live:
      backend: live
      model: gpt-live-1
      credentials_service: openai
      voice: marin
  agents:
    openclaw: live
```

The `stall` scenario blocks the instance's event loop with `sys.remote_exec`, which is why `run_instance.sh` opts the process into same-user ptrace.
