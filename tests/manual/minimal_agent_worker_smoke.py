"""Real Docker/CLI release workflow using fake credentials and a scripted SDK.

Run from the repository root with ``just test-minimal-agent-worker-smoke IMAGE DIR``
or ``uv run python -m tests.manual.minimal_agent_worker_smoke --image IMAGE --evidence-dir DIR``.
DIR must be persistent, outside temporary directories. Only containers with this
run's unique prefix are removed. No external provider or Matrix server is used.
"""

# ruff: noqa: ANN001, ANN202
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import shlex
import socket
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import docker
import httpx
import pytest
import uvicorn
import yaml

from mindroom import ai
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_cli.worker_protocol import CLI_PRIVATE_ROOT_PATH
from mindroom.agent_modes import resolve_agent_mode
from mindroom.agent_storage import create_session_storage
from mindroom.api.agent_cli import bind_agent_cli_registry
from mindroom.commands import mode_commands
from mindroom.config.agent import AgentConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import setup_logging
from mindroom.message_target import MessageTarget
from mindroom.response_turn import apply_exact_approval_decisions
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.runtime import shutdown_primary_worker_manager
from tests.conftest import bind_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import PLUGIN, ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context


def _ip(container):
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]


def _call(toolkit, function, arguments):

    payload = shlex.quote(json.dumps(arguments))
    return (
        f"status=0; receipt=$(mindroom-agent tools call {toolkit} {function} --json {payload}) || status=$?; "
        'test "$status" -eq 0 -o "$status" -eq 3 || { printf "%s\\n" "$receipt"; exit 20; }; '
        'call_id=$(printf "%s" "$receipt" | jq -r .call_id); mindroom-agent calls wait "$call_id"'
    )


class CaptureAudit:
    """Retain and verify every normal-output sink, with explicit capture identities."""

    def __init__(self, directory, secrets) -> None:
        self.directory = directory
        self.secrets = secrets
        self.records = []
        self.errors = []
        self.responses = []
        self.child_responses = []
        self.expected_workers = set()

    def capture(self, kind, identity, payload) -> None:
        """Retain raw bytes for verification and sanitized artifacts for review."""
        raw = payload if isinstance(payload, bytes) else str(payload).encode()
        name = f"capture-{len(self.records):03d}-{kind}.txt"
        self.records.append({"kind": kind, "identity": identity, "artifact": name, "raw": raw})
        # Preserve failure evidence without copying any detected secret into it.
        safe = raw
        for secret in self.secrets:
            safe = safe.replace(secret.encode(), b"[REDACTED]")
        (self.directory / name).write_bytes(safe)

    def capture_primary(self, logs_dir) -> None:
        """Read complete primary files produced by normal setup_logging."""
        for path in sorted(logs_dir.glob("*.log")):
            self.capture("primary", str(path), path.read_bytes())

    def capture_response(self, mode, presentation, trace, *, entity_name="helper") -> None:
        """Keep every presentation and trace, including earlier minimal output."""
        identity = f"{entity_name}-response-{len(self.responses) + len(self.child_responses)}-{mode}"
        if entity_name == "helper":
            self.responses.append(mode)
        else:
            self.child_responses.append(entity_name)
        self.capture("presentation", identity, presentation)
        self.capture("trace", identity, json.dumps(trace, default=str))

    def manifest(self) -> list[dict]:
        """Identify captured sinks and their unmodified byte lengths/digests."""
        return [
            {
                **{key: value for key, value in record.items() if key != "raw"},
                "bytes": len(record["raw"]),
                "sha256": hashlib.sha256(record["raw"]).hexdigest(),
            }
            for record in self.records
        ]

    def verify(self) -> None:
        """Reject leaks, capture failures, or missing execution evidence."""
        if self.errors:
            message = "Smoke capture/cleanup failures"
            raise ExceptionGroup(message, self.errors)
        leaked = [
            f"{record['kind']}:{record['identity']}"
            for record in self.records
            if any(secret.encode() in record["raw"] for secret in self.secrets)
        ]
        assert not leaked, f"Secret detected in captured sinks: {leaked}"
        observed = {record["kind"] for record in self.records if record["raw"]}
        required = {"worker", "primary", "primary-console", "presentation", "trace", "provider", "history"}
        assert required <= observed, f"Missing captured execution sinks: {required - observed}"
        captured_workers = {
            record["identity"].split(":", 1)[0] for record in self.records if record["kind"] == "worker"
        }
        assert self.expected_workers, "Missing expected workflow workers"
        assert self.expected_workers <= captured_workers, "Missing expected workflow worker logs"
        assert self.responses == ["standard", "minimal", "standard"], self.responses
        assert self.child_responses == ["code"], "Missing delegated child presentation"
        assert any(record["kind"] == "primary" and b"mindroom.ai" in record["raw"] for record in self.records), (
            "Missing actual primary response logging"
        )
        assert any(
            record["kind"] == "trace" and "minimal" in record["identity"] and b"bash" in record["raw"]
            for record in self.records
        ), "Missing actual minimal tool trace"


def capture_worker_before_removal(audit, container, remove) -> None:
    """Observe complete worker stdout/stderr before preserving original removal."""
    try:
        audit.capture("worker", container.id + ":" + container.name, container.logs(stdout=True, stderr=True))
    except Exception as exc:
        audit.errors.append(exc)
    finally:
        remove()


def cleanup_containers(client, prefix, audit) -> bool:
    """Attempt every owned container even if capture/removal of another fails."""
    try:
        containers = client.containers.list(all=True)
    except Exception as exc:
        audit.errors.append(exc)
        return False
    for container in containers:
        if not container.name.startswith(prefix):
            continue
        try:
            audit.capture("cleanup-container", container.id + ":" + container.name, container.logs())
        except Exception as exc:
            audit.errors.append(exc)
        finally:
            try:
                container.remove(force=True)
            except Exception as exc:
                audit.errors.append(exc)
    try:
        return not any(container.name.startswith(prefix) for container in client.containers.list(all=True))
    except Exception as exc:
        audit.errors.append(exc)
        return False


async def smoke(image, evidence_dir) -> None:  # noqa: C901, PLR0912, PLR0915 - explicit workflow cleanup
    """Run the real Docker worker parity workflow and retain its evidence."""
    evidence_dir = evidence_dir.resolve()
    if any(evidence_dir.is_relative_to(Path(root)) for root in ("/tmp", "/private/tmp")):  # noqa: S108 - reject temporary storage
        message = "Evidence directory must be persistent"
        raise ValueError(message)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"mindroom-parity-{uuid4().hex[:12]}"
    data = evidence_dir / prefix
    data.mkdir()
    client = docker.from_env()
    admin, provider_key, control = (f"fake-{name}-{uuid4().hex}" for name in ("admin", "provider", "control"))
    secret_values = [admin, provider_key, control]
    results = {"image": image, "prefix": prefix, "steps": [], "status": "failed"}
    audit = CaptureAudit(data, secret_values)
    failures = []
    runtime_logs = None
    runtime_output = io.StringIO()
    stream_capture = ExitStack()
    stream_capture.enter_context(redirect_stdout(runtime_output))
    stream_capture.enter_context(redirect_stderr(runtime_output))
    server = None
    serve = None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    patch = pytest.MonkeyPatch()
    original_remove = DockerWorkerBackend._remove_container

    def remove_with_capture(backend, container):
        if container is not None and container.name.startswith(prefix):
            capture_worker_before_removal(audit, container, lambda: original_remove(backend, container))
        else:
            original_remove(backend, container)

    patch.setattr(DockerWorkerBackend, "_remove_container", remove_with_capture)
    try:
        primary_dir = data / "primary"
        primary_dir.mkdir()
        socket_path = primary_dir / "api.sock"
        previous = Path.cwd()
        os.chdir(primary_dir)
        try:
            sock.bind("api.sock")
        finally:
            os.chdir(previous)
        socket_path.chmod(0o666)
        primary_conf = primary_dir / "nginx.conf"
        primary_conf.write_text(
            "events {}\nhttp { access_log off; upstream api { server unix:/smoke/api.sock; } server { listen 8080; location / { proxy_pass http://api; } } }\n",
        )
        primary = client.containers.run(
            "nginx:1.28-alpine",
            name=f"{prefix}-primary",
            detach=True,
            volumes={
                str(primary_dir): {"bind": "/smoke", "mode": "ro"},
                str(primary_conf): {"bind": "/etc/nginx/nginx.conf", "mode": "ro"},
            },
        )
        primary_url = f"http://{_ip(primary)}:8080"
        gateway_conf = data / "gateway.conf"
        gateway_conf.write_text(
            "events {}\nhttp { access_log off; server { listen 8080;\n"
            "location = /api/agent-cli/operations { if ($request_method != POST) { return 405; } proxy_pass "
            + primary_url
            + "; }\n"
            "location ~ ^/api/agent-cli/calls/[a-zA-Z0-9_-]+$ { if ($request_method != GET) { return 405; } proxy_pass "
            + primary_url
            + "; }\n"
            "location / { return 404; } } }\n",
        )
        gateway = client.containers.run(
            "nginx:1.28-alpine",
            name=f"{prefix}-gateway",
            detach=True,
            volumes={
                str(gateway_conf): {"bind": "/etc/nginx/nginx.conf", "mode": "ro"},
            },
        )
        gateway_url = f"http://{_ip(gateway)}:8080"
        plugin_dir = data / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "mindroom.plugin.json").write_text(
            json.dumps({"name": "parity", "tools_module": "tools.py", "skills": []}),
        )
        (plugin_dir / "tools.py").write_text(PLUGIN)
        runtime = _runtime_context(data)
        runtime = replace(
            runtime,
            target=MessageTarget.resolve(
                runtime.target.room_id,
                runtime.target.source_thread_id,
                runtime.target.reply_to_event_id,
            ),
        )
        paths = resolve_primary_runtime_paths(
            config_path=data / "config.yaml",
            storage_path=data / "state",
            process_env={
                "MINDROOM_API_KEY": admin,
                "MINDROOM_AGENT_CLI_GATEWAY_URL": gateway_url,
                "MINDROOM_AGENT_CLI_PRIMARY_URL": primary_url,
                "MINDROOM_WORKER_BACKEND": "docker",
                "MINDROOM_SANDBOX_PROXY_TOKEN": control,
                "MINDROOM_DOCKER_WORKER_IMAGE": image,
                "MINDROOM_DOCKER_WORKER_NAME_PREFIX": prefix,
                "MINDROOM_DOCKER_WORKER_USER": f"{os.getuid()}:{os.getgid()}",
                "MINDROOM_DOCKER_WORKER_READY_TIMEOUT_SECONDS": "90",
            },
        )
        setup_logging(runtime_paths=paths)
        runtime_logs = paths.storage_root / "logs"
        config = runtime.config

        config.plugins = [PluginEntryConfig(path=str(plugin_dir))]
        config.administrators = [runtime.requester_id]
        config.agents["helper"] = AgentConfig(
            display_name="Helper",
            tools=["shell", "file", "parity"],
            delegate_to=["code"],
            learning=False,
            memory_backend="file",
        )
        config.agents["code"] = AgentConfig(display_name="Code", tools=[], learning=False, memory_backend="file")
        bind_runtime_paths(config, paths)
        paths.config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
        persist_entity_accounts(config, paths)
        get_runtime_credentials_manager(paths).save_credentials("parity", {"api_key": provider_key})
        registry = TurnToolRegistry()
        approvals = []
        tokens = []

        async def approve(paused):
            assert paused.cli_call["toolkit"] == "parity"
            assert paused.cli_call["function"] == "approved"
            assert paused.cli_call["arguments"] == {"value": "exact value"}
            approvals.append(paused.cli_call["call_id"])
            owner = registry._owners[0]
            lease = owner._worker
            worker = client.containers.get(lease.handle.debug_metadata["container_id"])
            audit.expected_workers.add(worker.id)
            worker.reload()
            assert all(secret not in json.dumps(worker.attrs) for secret in secret_values)
            assert all(
                not Path(CLI_PRIVATE_ROOT_PATH).is_relative_to(Path(mount["Destination"]))
                for mount in worker.attrs["Mounts"]
            )
            scan = await asyncio.to_thread(
                worker.exec_run,
                [
                    "sh",
                    "-c",
                    "cat /proc/self/environ; find /app/worker /app/config-host -type f -exec cat {} + 2>/dev/null",
                ],
            )
            assert not any(secret.encode() in scan.output for secret in secret_values)
            token_result = await asyncio.to_thread(worker.exec_run, ["cat", f"{CLI_PRIVATE_ROOT_PATH}/capability"])
            assert token_result.exit_code == 0
            token = token_result.output.decode().strip()
            assert token
            assert token.encode() not in scan.output
            tokens.append(token)
            secret_values.append(token)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as probe:
                headers = {"Authorization": "Bearer " + token}
                forged = await probe.post(
                    "/api/agent-cli/operations",
                    headers=headers,
                    json={"operation": "tools.list", "requester_id": "@other:test"},
                )
                assert forged.status_code == 422
                missing = await probe.get(f"/api/agent-cli/calls/{uuid4()}", headers=headers)
                assert missing.status_code == 401
            results["steps"].append(
                "real worker mount/env credential scan; private token mount; forged selector and wrong call ID rejected",
            )
            return tuple(
                apply_exact_approval_decisions(
                    paused.requirements,
                    decisions={str(tool.tool_call_id): True for tool in paused.tools},
                    denial_reasons={str(tool.tool_call_id): None for tool in paused.tools},
                ),
            )

        runtime = replace(
            runtime,
            runtime_paths=paths,
            config=config,
            orchestrator=SimpleNamespace(agent_cli_registry=registry),
            cli_approval_handler=approve,
        )
        from mindroom.api.main import app, initialize_api_app  # noqa: PLC0415 - resolve runtime before app setup

        initialize_api_app(app, paths)
        bind_agent_cli_registry(app, registry)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        serve = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if serve.done():
                await serve
            await asyncio.sleep(0.01)
        provider = ScriptedProvider()
        provider.install(patch)
        original_response = ai.ai_response

        async def response_with_capture(ctx, *args, **kwargs):  # noqa: ANN002, ANN003
            trace = kwargs.setdefault("tool_trace_collector", [])
            presentation = await original_response(ctx, *args, **kwargs)
            audit.capture_response(
                ctx.agent_mode,
                presentation,
                [asdict(entry) for entry in trace],
                entity_name=ctx.entity_label,
            )
            return presentation

        patch.setattr(ai, "ai_response", response_with_capture)
        identity = build_execution_identity_from_runtime_context(runtime)
        storage = create_session_storage("helper", config, paths, identity)

        state_root = resolve_agent_storage("helper", config, paths, identity).state_root
        command_args = {
            "config": config,
            "runtime_paths": paths,
            "target": runtime.target,
            "requester_id": runtime.requester_id,
            "membership_index": runtime.agent_reply_memberships,
        }

        async def respond(prompt):
            mode = resolve_agent_mode(state_root, "helper", runtime.session_id)
            with tool_runtime_context(runtime):
                return await ai.ai_response(
                    replace(_turn_context(), session_id=runtime.session_id, agent_mode=mode, run_id=None),
                    prompt=prompt,
                    runtime_paths=paths,
                    config=config,
                    execution_identity=identity,
                    supports_native_tool_approval=True,
                    show_tool_calls=True,
                )

        provider.steps = ["standard history sentinel"]
        initial = await respond("Remember this same-conversation history sentinel")
        assert "standard history sentinel" in str(initial), initial
        results["steps"].append("standard response persisted")
        assert "uses `minimal`" in mode_commands.handle_mode_command("helper minimal", **command_args)
        command = "\n".join(
            [
                "set -e",
                "mindroom-agent tools list",
                "mindroom-agent tools describe parity integration",
                'test -r "$MINDROOM_AGENT_CLI_TOKEN_PATH"',
                f'case "$MINDROOM_AGENT_CLI_TOKEN_PATH" in {CLI_PRIVATE_ROOT_PATH}/*) ;; *) exit 31;; esac',
                'test -z "${OPENAI_API_KEY:-}${MINDROOM_API_KEY:-}${MINDROOM_SANDBOX_PROXY_TOKEN:-}"',
                _call("parity", "integration", {"digest": hashlib.sha256(provider_key.encode()).hexdigest()}),
                _call("parity", "approved", {"value": "exact value"}),
                _call("delegate", "run_subagent", {"agent_name": "code", "task": "Return child done"}),
                _call("parity", "media", {}),
                "printf '%s\\n' 'same-agent persistent note' > smoke-note.md",
                "cat smoke-note.md",
            ],
        )
        provider.steps = [[("bash", {"command": command})], "child done", "minimal done"]
        result = await respond("Discover, integrate, approve, delegate, attach, and write note")
        assert "minimal done" in str(result), result
        assert len(approvals) == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as probe:
            expired = await probe.post(
                "/api/agent-cli/operations",
                headers={"Authorization": "Bearer " + tokens[0]},
                json={"operation": "tools.list"},
            )
            assert expired.status_code == 401
        results["steps"].append("retired response grant rejected by actual API")
        assert len(provider.requests) == 4, len(provider.requests)
        minimal_requests = [provider.requests[1], provider.requests[3]]
        assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in minimal_requests)
        projection = str(provider.requests[3]["messages"])
        for expected in (
            "primary credential accepted",
            "approved:exact value",
            "child done",
            "same-agent persistent note",
            "image_url",
        ):
            assert expected in projection, expected
        results["steps"].extend(
            [
                "minimal Bash-only requests",
                "CLI discovery",
                "main-only credential integration",
                "exact approval",
                "native delegated child",
                "image/file result",
                "workspace note",
            ],
        )
        assert "uses `standard`" in mode_commands.handle_mode_command("helper standard", **command_args)
        provider.steps = [[("read_file", {"file_name": "smoke-note.md"})], "standard done"]
        result = await respond("Read the same note and keep earlier history")
        assert "standard done" in str(result)
        final_request = provider.requests[-1]
        assert "same-agent persistent note" in str(final_request["messages"])
        assert "standard history sentinel" in str(final_request["messages"])
        assert any(tool["function"]["name"] == "read_file" for tool in final_request["tools"])
        assert len(storage.get_session(runtime.session_id).runs) == 3
        results["steps"].append("standard mode reads identical workspace note and prior history")
        audit.capture("provider", "all-sdk-requests", json.dumps(provider.requests, indent=2, default=str))
        audit.capture(
            "history",
            runtime.session_id,
            json.dumps(storage.get_session(runtime.session_id).to_dict(), default=str),
        )
        results["requests"] = len(provider.requests)
    except BaseException as exc:
        failures.append(exc)
    finally:
        for operation in (
            storage.close if "storage" in locals() else lambda: None,
            shutdown_primary_worker_manager,
            patch.undo,
        ):
            try:
                operation()
            except Exception as exc:
                failures.append(exc)
        if server is not None:
            server.should_exit = True
        if serve is not None:
            try:
                await serve
            except BaseException as exc:
                failures.append(exc)
        try:
            sock.close()
        except Exception as exc:
            failures.append(exc)
        results["cleanup"] = cleanup_containers(client, prefix, audit)
        try:
            logging.shutdown()
        except Exception as exc:
            failures.append(exc)
        finally:
            stream_capture.close()
        try:
            audit.capture("primary-console", "primary-process-stdout-stderr", runtime_output.getvalue())
            if runtime_logs is not None:
                audit.capture_primary(runtime_logs)
            audit.verify()
            assert results["cleanup"], "Owned containers remain after cleanup"
        except Exception as exc:
            failures.append(exc)
        results["captures"] = audit.manifest()
        results["expected_worker_ids"] = sorted(audit.expected_workers)
        results["response_modes"] = audit.responses
        results["child_responses"] = audit.child_responses
        results["failures"] = [type(exc).__name__ for exc in failures]
        if not failures:
            results["status"] = "passed"
            results["steps"].append(
                "all worker/runtime logs, presentations, traces, SDK requests and history secret-checked",
            )
        (data / "result.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results, indent=2))
    if len(failures) == 1:
        raise failures[0]
    if failures:
        message = "Smoke workflow and cleanup failures"
        raise BaseExceptionGroup(message, failures)


def main() -> None:
    """Parse the worker image and persistent evidence directory for the manual run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(smoke(args.image, args.evidence_dir))


if __name__ == "__main__":
    main()
