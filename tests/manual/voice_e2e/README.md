# Voice-call end-to-end harness

Runs real GPT-Live voice calls against a local MatrixRTC stack and a separate MindRoom instance, with a scripted synthetic caller.
Each call costs real GPT-Live minutes and delegate-model tokens.

1. Create `~/.mindroom-voicetest/` (mode 700) with `config.yaml`, `.env` (provider keys, `MATRIX_HOMESERVER=http://localhost:8008`, `MATRIX_SERVER_NAME=localhost`, `SSL_CERT_FILE` pointing at a CA bundle so aiohttp can reach OpenAI), and `stack.env` (`LIVEKIT_NODE_IP=<LAN IP>`, `LIVEKIT_SECRET=<48 hex chars>`).
   The config needs `calls.livekit_service_url: http://127.0.0.1:8781` and an agent whose access list includes `@voicecaller:localhost`.
2. Start the stack: `docker compose --env-file ~/.mindroom-voicetest/stack.env -f tests/manual/voice_e2e/compose.yaml up -d --wait`.
3. Start the instance with the production Python version: `tests/manual/voice_e2e/instance_ctl.sh start` (expects `.venv314`; `MINDROOM_VOICETEST_REPO` selects another checkout).
4. Run a scenario: `.venv314/bin/python tests/manual/voice_e2e/voice_call_e2e.py baseline` (also `latency`, `probe`, `queue`, `stale_queue`, `stall`, `delegated_barge`).
5. Summarize: `python tests/manual/voice_e2e/analyze.py ~/.mindroom-voicetest/runs/<run>`.

The `stall` scenario blocks the instance's event loop with `sys.remote_exec`, which is why `run_instance.sh` opts the process into same-user ptrace.
