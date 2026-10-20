"""`mindroom agents rename-account`, and the plan, apply and output helpers both rename commands share."""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - Typer reads the option annotations at runtime
from typing import TYPE_CHECKING

import typer
from rich.markup import escape

from mindroom.constants import matrix_state_file, runtime_matrix_homeserver, runtime_matrix_ssl_verify

from .config import activate_cli_runtime, console, format_validation_errors, load_config_quiet

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

    from mindroom.agent_rename.steps import ApplyResult, RenamePlan
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.state import MatrixAccount

_LOGIN_CHECK_DEVICE_NAME = "mindroom rename check"
_LOGIN_CHECK_TIMEOUT_SECONDS = 30.0
_REJECTED_LOGIN_STATUSES = frozenset({401, 403})
_UNDO_FAILED_EXIT_CODE = 2


@dataclass(frozen=True)
class _Adoption:
    """One agent's stored account and the ID the server renamed it to."""

    agent: str
    old_user_id: str
    new_user_id: str
    stored_user_id: str
    password: str | None


def rename_account(
    agent: str = typer.Argument(..., help="The agent's name in the config."),
    new_user_id: str = typer.Argument(..., help="The Matrix ID the server renamed the agent's account to."),
    from_user_id: str | None = typer.Option(
        None,
        "--from",
        metavar="OLD_USER_ID",
        help=(
            "The Matrix ID the server renamed the account from, when matrix_state.yaml already holds the new one "
            "because its username was changed by hand."
        ),
    ),
    apply: bool = typer.Option(False, "--apply", help="Make the changes; without it the plan is only printed."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking for confirmation."),
    discard_new_principal: bool = typer.Option(
        False,
        "--discard-new-principal",
        help=(
            "Delete what MindRoom created under the new ID before its state was moved: "
            "the new principal's event journal rows, only while the old principal still has rows to move, "
            "and new-ID state that holds no credentials."
        ),
    ),
    skip_login_check: bool = typer.Option(
        False,
        "--skip-login-check",
        help="Apply without first logging in as the new ID with the stored password.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data.",
    ),
) -> None:
    """Adopt an agent's Matrix account after the server renamed it.

    Run it with MindRoom stopped, after the homeserver renamed the account in place
    (for example with Tuwunel's `query user-id-rewrite`). It moves credentials, workers,
    and other state kept under the old Matrix ID to the new one, rewrites the event journal,
    and replaces the old ID in the config. The agent's name in the config stays.
    """
    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    config = load_config_or_exit(runtime_paths)
    with exit_on_rename_errors():
        _rename_account(
            runtime_paths,
            config,
            agent=agent,
            new_user_id=new_user_id,
            from_user_id=from_user_id,
            apply=apply,
            yes=yes,
            discard_new_principal=discard_new_principal,
            skip_login_check=skip_login_check,
        )


@contextmanager
def exit_on_rename_errors() -> Iterator[None]:
    """Print a refused, failed, or half-undone rename and exit with its code: 1, or 2 when the disk needs a restore."""
    from mindroom.agent_rename.steps import (  # noqa: PLC0415 - keeps the rename engine out of CLI import time
        RenameApplyError,
        RenameRefusedError,
        RenameUndoError,
    )

    try:
        yield
    except RenameRefusedError as exc:
        say(str(exc), label="[red]Refused:[/red] ")
        raise typer.Exit(1) from None
    except RenameApplyError as exc:
        say(str(exc), label="[red]Not applied:[/red] ")
        raise typer.Exit(1) from None
    except RenameUndoError as exc:
        say(str(exc), label="[red]Undo failed:[/red] ")
        say(f"Do not start MindRoom before restoring the remaining steps listed in {exc.manifest} by hand.")
        raise typer.Exit(_UNDO_FAILED_EXIT_CODE) from None


def say(text: str, *, label: str = "") -> None:
    """Print ``text`` literally after an optional styled ``label``.

    Lines are never wrapped, so paths stay copyable, and ``:name:`` emoji codes stay text:
    Matrix IDs followed by a colon, like ``@old:x:``, contain them.
    """
    console.print(f"{label}{escape(text)}", soft_wrap=True, emoji=False)


def load_config_or_exit(runtime_paths: RuntimePaths) -> Config:
    """Load the config, or exit with its errors.

    Unlike other commands, a missing config is not seeded from a template: that would be a write,
    and without a config there is no agent to rename.
    """
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415

    config_path = runtime_paths.config_path
    if not config_path.exists():
        say(f"No config at {config_path}.", label="[red]Error:[/red] ")
        raise typer.Exit(1)
    try:
        return load_config_quiet(runtime_paths, tolerate_plugin_load_errors=True)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None


def _rename_account(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    agent: str,
    new_user_id: str,
    from_user_id: str | None,
    apply: bool,
    yes: bool,
    discard_new_principal: bool,
    skip_login_check: bool,
) -> None:
    from mindroom.agent_rename.guard import require_supported_backends, stopped_runtime  # noqa: PLC0415
    from mindroom.credentials.manager import scoped_credentials_dir_part  # noqa: PLC0415
    from mindroom.desktop.native_config import native_config_path  # noqa: PLC0415

    adoption = _adoption(runtime_paths, config, agent=agent, new_user_id=new_user_id, from_user_id=from_user_id)
    if adoption is None:
        say("Already adopted; nothing to do.")
        return
    require_supported_backends(config, runtime_paths)
    if not apply:
        # Taking MindRoom's locks would create lock files, so a dry run plans without them.
        plan = _plan(runtime_paths, config, adoption, discard_new_principal=discard_new_principal)
        print_plan(_adoption_header(adoption), plan)
        if not plan.steps:
            say(f"Nothing left to move from {adoption.old_user_id}; nothing was changed.")
            return
        say("Dry run: nothing was changed. Rerun with --apply to make these changes.")
        say(
            "--apply plans again under MindRoom's locks. Event journal collisions in tables other than "
            "room_member_joins only show up when --apply rewrites the journal; "
            "if one does, that rewrite rolls back and the command refuses.",
        )
        return
    with stopped_runtime(runtime_paths):
        if not skip_login_check:
            _check_login(runtime_paths, adoption)
        plan = _plan(runtime_paths, config, adoption, discard_new_principal=discard_new_principal)
        print_plan(_adoption_header(adoption), plan)
        if not plan.steps:
            # Only with --from: otherwise the stored username still needs its step.
            say(f"Nothing left to move from {adoption.old_user_id}; nothing was changed.")
            return
        confirm_plan(yes=yes)
        result = apply_rename_plan(plan, kind="rename-account")
        say(f"agent {agent} now uses {adoption.new_user_id}.", label="[green]Adopted:[/green] ")
        print_applied(result)
        storage = runtime_paths.storage_root
        old_user_id = adoption.old_user_id
        # The plan reports a desktop bridge pinned to the old ID as staying.
        # It reports the old crypto store too, but the scan never enters encryption_keys.
        print_leftovers(
            storage,
            old_user_id,
            needles=(old_user_id, scoped_credentials_dir_part(old_user_id)),
            expected=frozenset({native_config_path(storage)}),
        )


def stored_accounts(runtime_paths: RuntimePaths) -> dict[str, MatrixAccount]:
    """Read the stored Matrix accounts; ``MatrixState.load`` can rewrite a legacy file, which a dry run must not."""
    import yaml  # noqa: PLC0415

    from mindroom import yaml_io  # noqa: PLC0415
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.matrix.state import MatrixState  # noqa: PLC0415

    path = matrix_state_file(runtime_paths)
    try:
        data = yaml_io.safe_load(path.read_bytes()) if path.exists() else None
        return MatrixState.model_validate(data or {}).accounts
    except (OSError, ValueError, yaml.YAMLError) as exc:
        msg = f"Cannot read {path}: {exc}"
        raise RenameRefusedError(msg) from exc


def _adoption(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    agent: str,
    new_user_id: str,
    from_user_id: str | None,
) -> _Adoption | None:
    """Check the request and return it, or ``None`` when nothing is left under another ID.

    The stored username alone does not prove an adoption: changing it by hand leaves the journal and files
    under the old ID. So when it already names the new ID, the journal decides, and ``--from`` names the old ID.
    """
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.matrix.identity import MatrixID, managed_account_key  # noqa: PLC0415

    state_path = matrix_state_file(runtime_paths)
    accounts = stored_accounts(runtime_paths)
    domain = config.get_domain(runtime_paths)
    user_ids = {
        key: MatrixID.from_username(account.username, account.domain or domain).full_id
        for key, account in accounts.items()
    }
    account_key = managed_account_key(agent)
    stored_user_id = user_ids.get(account_key)
    # Checked before anything else that can refuse, so a rerun after a successful apply is a no-op.
    if from_user_id is None and stored_user_id == new_user_id:
        _refuse_journal_under_other_ids(runtime_paths, agent=agent, new_user_id=new_user_id)
        return None
    if agent not in config.agents:
        msg = f"{agent} is not an agent in {runtime_paths.config_path}."
        raise RenameRefusedError(msg)
    if stored_user_id is None:
        msg = f"{state_path} holds no account {account_key}, so there is no Matrix account to adopt."
        raise RenameRefusedError(msg)
    old_user_id = _old_user_id(
        state_path,
        account_key,
        stored_user_id=stored_user_id,
        new_user_id=new_user_id,
        from_user_id=from_user_id,
    )
    old_domain = MatrixID.parse(old_user_id).domain
    if MatrixID.parse(new_user_id).domain != old_domain:
        msg = f"{new_user_id} is not on {old_domain}, the server of {old_user_id}; a rename in place keeps the server."
        raise RenameRefusedError(msg)
    owner = _other_owner(user_ids, account_key, new_user_id)
    if owner is not None:
        msg = f"{new_user_id} is already the stored account {owner} in {state_path}."
        raise RenameRefusedError(msg)
    owner = _other_owner(user_ids, account_key, old_user_id)
    if owner is not None:
        msg = f"--from {old_user_id} is the stored account {owner} in {state_path}, not a former ID of {account_key}."
        raise RenameRefusedError(msg)
    if old_user_id != stored_user_id:
        _require_former_user_id(runtime_paths, agent=agent, from_user_id=old_user_id, new_user_id=new_user_id)
    return _Adoption(agent, old_user_id, new_user_id, stored_user_id, accounts[account_key].password)


def _old_user_id(
    state_path: Path,
    account_key: str,
    *,
    stored_user_id: str,
    new_user_id: str,
    from_user_id: str | None,
) -> str:
    """Check both IDs and return the one to adopt from: ``--from`` when given, otherwise the stored account's."""
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.matrix.identity import parse_historical_matrix_user_id  # noqa: PLC0415

    for user_id in (new_user_id, from_user_id):
        try:
            if user_id is not None:
                parse_historical_matrix_user_id(user_id)
        except ValueError as exc:
            msg = f"{user_id} is not a Matrix user ID ({exc})."
            raise RenameRefusedError(msg) from exc
    if from_user_id is None:
        return stored_user_id
    if from_user_id == new_user_id:
        msg = f"--from {from_user_id} is the new ID itself; pass the ID the server renamed the account from."
        raise RenameRefusedError(msg)
    if stored_user_id not in (from_user_id, new_user_id):
        msg = (
            f"--from {from_user_id} does not match the stored account {stored_user_id} of {account_key} "
            f"in {state_path}; --from is for a stored account that already holds {new_user_id}."
        )
        raise RenameRefusedError(msg)
    return from_user_id


def _require_former_user_id(runtime_paths: RuntimePaths, *, agent: str, from_user_id: str, new_user_id: str) -> None:
    """Refuse a ``--from`` that nothing shows to be a former Matrix ID of ``agent``.

    ``--from`` moves whatever is stored under that ID, so a wrong one, such as a person's, would hand their
    OAuth credentials and journal mentions to the agent. Accepted are an ID the journal keys the agent's rows by,
    and the ID MindRoom generates for the agent on the new ID's server, which lets a killed run resume.
    """
    from mindroom.agent_rename.journal import other_principal_user_ids  # noqa: PLC0415
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415
    from mindroom.matrix.identity import MatrixID  # noqa: PLC0415
    from mindroom.matrix_identifiers import agent_username_localpart  # noqa: PLC0415

    database = event_journal_sqlite_path(runtime_paths.storage_root)
    if from_user_id in other_principal_user_ids(database, agent=agent, user_id=new_user_id):
        return
    generated = MatrixID.from_username(
        agent_username_localpart(agent, runtime_paths),
        MatrixID.parse(new_user_id).domain,
    ).full_id
    if from_user_id == generated:
        return
    msg = (
        f"--from {from_user_id} must be a former Matrix ID of agent {agent}, and nothing shows that it is: "
        f"the event journal {database} has no rows for {agent}@{from_user_id}, "
        f"and it is not {generated}, the ID MindRoom generates for {agent}. "
        "If an earlier run with this --from was killed after its journal step, restore from its backup manifest "
        "before running again."
    )
    raise RenameRefusedError(msg)


def _other_owner(user_ids: dict[str, str], account_key: str, user_id: str) -> str | None:
    """Return the stored account other than ``account_key`` that has ``user_id``, if any."""
    return next((key for key, stored in user_ids.items() if key != account_key and stored == user_id), None)


def _refuse_journal_under_other_ids(runtime_paths: RuntimePaths, *, agent: str, new_user_id: str) -> None:
    """Refuse when the journal still keys ``agent``'s rows by an ID other than the stored ``new_user_id``.

    That is what changing the stored username by hand leaves behind: MindRoom then runs as the new ID,
    and its sync fails on the old rows with ``IngestionBatchIntegrityError``.
    """
    from mindroom.agent_rename.journal import other_principal_user_ids  # noqa: PLC0415
    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415
    from mindroom.matrix.identity import MatrixID, try_parse_historical_matrix_user_id  # noqa: PLC0415

    database = event_journal_sqlite_path(runtime_paths.storage_root)
    server = MatrixID.parse(new_user_id).domain
    # Only an ID on the new ID's server can have been renamed in place, and --from refuses any other.
    others = {
        user_id: counts
        for user_id, counts in other_principal_user_ids(database, agent=agent, user_id=new_user_id).items()
        if (parsed := try_parse_historical_matrix_user_id(user_id)) is not None
        and MatrixID.parse(parsed).domain == server
    }
    if not others:
        return
    found = "; ".join(f"{agent}@{user_id} ({counts})" for user_id, counts in others.items())
    storage_options = ["--config", str(runtime_paths.config_path), "--storage-path", str(runtime_paths.storage_root)]
    commands = (
        shlex.join(["mindroom", "agents", "rename-account", agent, new_user_id, "--from", user_id, *storage_options])
        for user_id in others
    )
    msg = (
        f"{matrix_state_file(runtime_paths)} already names {new_user_id} for agent_{agent}, "
        f"but the event journal {database} still has rows for {found}, so its state was never moved. "
        f"If the server renamed that account to {new_user_id}, run this to move its state:"
        + "".join(f"\n  {command}" for command in commands)
    )
    raise RenameRefusedError(msg)


def _check_login(runtime_paths: RuntimePaths, adoption: _Adoption) -> None:
    """Refuse unless the homeserver accepts the new ID with the stored password, then log that session out again.

    Plain HTTP on purpose: MindRoom's Matrix login creates a local crypto store for the ID,
    which would collide with the one MindRoom creates when it logs in after the rename.
    """
    import httpx  # noqa: PLC0415

    from mindroom.agent_rename.steps import RenameRefusedError  # noqa: PLC0415

    new_user_id = adoption.new_user_id
    if adoption.password is None:
        msg = (
            f"{matrix_state_file(runtime_paths)} holds no password for agent_{adoption.agent}, so the login check "
            "cannot run; pass --skip-login-check once the server renamed the account."
        )
        raise RenameRefusedError(msg)
    homeserver = runtime_matrix_homeserver(runtime_paths).rstrip("/")
    login = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": new_user_id},
        "password": adoption.password,
        "initial_device_display_name": _LOGIN_CHECK_DEVICE_NAME,
    }
    verify = runtime_matrix_ssl_verify(runtime_paths=runtime_paths)
    with httpx.Client(verify=verify, timeout=_LOGIN_CHECK_TIMEOUT_SECONDS) as client:
        try:
            response = client.post(f"{homeserver}/_matrix/client/v3/login", json=login)
        except httpx.HTTPError as exc:
            msg = f"Cannot reach {homeserver} for the login check ({exc}); retry, or pass --skip-login-check."
            raise RenameRefusedError(msg) from exc
        if response.status_code in _REJECTED_LOGIN_STATUSES:
            msg = (
                f"The server does not accept {new_user_id} with the stored password; "
                "rename the account on the server first."
            )
            raise RenameRefusedError(msg)
        if not response.is_success:
            msg = f"The login check for {new_user_id} failed with HTTP {response.status_code}; retry, or pass --skip-login-check."
            raise RenameRefusedError(msg)
        _log_out(client, homeserver, response)


def _log_out(client: httpx.Client, homeserver: str, login: httpx.Response) -> None:
    """End the login check's session; if that fails, only a stray device stays behind, so it warns."""
    import httpx  # noqa: PLC0415

    try:
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.post(f"{homeserver}/_matrix/client/v3/logout", headers=headers).raise_for_status()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        say(
            f"Could not log the login check out ({exc}); "
            f"remove its device {_LOGIN_CHECK_DEVICE_NAME!r} from the account by hand.",
            label="[yellow]Warning:[/yellow] ",
        )


def _plan(
    runtime_paths: RuntimePaths,
    config: Config,
    adoption: _Adoption,
    *,
    discard_new_principal: bool,
) -> RenamePlan:
    from mindroom.agent_rename.account_files import account_file_steps, matrix_state_steps  # noqa: PLC0415
    from mindroom.agent_rename.config_edit import config_value_steps  # noqa: PLC0415
    from mindroom.agent_rename.journal import journal_account_steps  # noqa: PLC0415
    from mindroom.agent_rename.steps import FinalAction, RenamePlan  # noqa: PLC0415
    from mindroom.event_journal.opening import event_journal_sqlite_path  # noqa: PLC0415

    old_user_id, new_user_id = adoption.old_user_id, adoption.new_user_id
    account_steps, account_reports = matrix_state_steps(
        runtime_paths,
        agent=adoption.agent,
        old_user_id=old_user_id,
        new_user_id=new_user_id,
    )
    journal_steps, journal_reports = journal_account_steps(
        event_journal_sqlite_path(runtime_paths.storage_root),
        agent=adoption.agent,
        old_user_id=old_user_id,
        new_user_id=new_user_id,
        discard_new_principal=discard_new_principal,
        # Without a step for matrix_state.yaml the account keeps the device it uses under the new ID.
        new_device_kept=not account_steps,
    )
    file_steps, reports = account_file_steps(
        runtime_paths,
        agent=adoption.agent,
        old_user_id=old_user_id,
        new_user_id=new_user_id,
        config=config,
        discard_new_identity_state=discard_new_principal,
    )
    config_steps = config_value_steps(runtime_paths.config_path, old_value=old_user_id, new_value=new_user_id)
    final_steps = [step for step in file_steps if isinstance(step, FinalAction)]
    # The journal edit goes first: some of its collisions only show up when it runs,
    # and a failing first step leaves nothing to undo.
    # The username goes last among the reversible steps: it is what marks the account adopted,
    # so a run killed before it never looks finished. Final actions always run after every reversible step.
    steps = (
        *journal_steps,
        *(step for step in file_steps if not isinstance(step, FinalAction)),
        *config_steps,
        *account_steps,
        *final_steps,
    )
    return RenamePlan(
        storage_root=runtime_paths.storage_root,
        steps=steps,
        reports=(*journal_reports, *account_reports, *reports),
    )


def _adoption_header(adoption: _Adoption) -> str:
    if adoption.stored_user_id == adoption.old_user_id:
        detail = f"whose stored account is {adoption.old_user_id}:"
    else:
        detail = f"from {adoption.old_user_id}; its stored account already is {adoption.new_user_id}:"
    return f"Plan to adopt {adoption.new_user_id} for agent {adoption.agent}, {detail}"


def print_plan(header: str, plan: RenamePlan) -> None:
    """Print ``header``, then every step and report of ``plan``."""
    say(header)
    for line in plan.describe():
        say(f"  - {line}")


def confirm_plan(*, yes: bool) -> None:
    """Ask before applying unless ``yes``; answering no exits 1."""
    if not yes and not typer.confirm("Apply this plan?"):
        say("Nothing was changed.")
        raise typer.Exit(1)


def apply_rename_plan(plan: RenamePlan, *, kind: str) -> ApplyResult:
    """Apply ``plan`` with backups named after ``kind``, then shut down the worker manager a retired worker leased."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from mindroom.agent_rename.steps import apply_plan  # noqa: PLC0415
    from mindroom.workers.backend import WorkerBackendError  # noqa: PLC0415
    from mindroom.workers.runtime import shutdown_primary_worker_manager  # noqa: PLC0415

    try:
        return apply_plan(plan, kind=kind, now=datetime.now(UTC))
    finally:
        # Retiring an old Docker worker leases the process-wide worker manager; shut it down so the command exits.
        try:
            shutdown_primary_worker_manager()
        except WorkerBackendError as exc:
            say(str(exc), label="[yellow]Warning:[/yellow] ")


def print_applied(result: ApplyResult) -> None:
    """Print where the backups went and the irreversible actions that failed."""
    say(f"Backups: {result.backup_dir}. Its manifest.json lists every change, for restoring by hand.")
    for warning in result.warnings:
        say(warning, label="[yellow]Warning:[/yellow] ")


def print_leftovers(
    storage: Path,
    subject: str,
    *,
    needles: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    expected: frozenset[Path],
) -> None:
    """Print what in the storage directory still names ``subject``, split by whether the plan reported it."""
    from mindroom.agent_rename.scan import scan_leftovers  # noqa: PLC0415

    report = scan_leftovers(storage, needles=needles, expected=expected, names=names)
    if not report.expected and not report.unexpected:
        say(f"Leftover scan: nothing in {storage} names {subject} any more.")
        return
    say(f"Leftover scan: places in {storage} that still name {subject}")
    if report.expected:
        say("  Reported above as staying:")
        for line in report.expected:
            say(f"    - {line}")
    if report.unexpected:
        say("  Not reported by the plan, or not scanned:")
        for line in report.unexpected:
            say(f"    - {line}")
