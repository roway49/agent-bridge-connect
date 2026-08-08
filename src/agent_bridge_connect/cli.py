from __future__ import annotations

import argparse
import difflib
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    DEFAULT_BOARD_ROOT,
    get_executor_config,
    init_board,
    load_config,
    resolve_runner_allowed_roots,
    resolve_workspace_root,
)
from .executor_registry import get_executor
from .path_model import DEFAULT_CUSTOMER_PATH, derive_customer_path_plan
from .permission_modes import CANONICAL_PERMISSION_MODES, PERMISSION_EXTENSION_KEY
from .protocol import ABCError
from .service import TaskService, load_steps, task_to_status
from .task_id import is_task_like, split_task_ref
from .terminal_states import TASK_TERMINAL_STATES, terminal_status_label

_TASK_TERMINAL_STATUSES = TASK_TERMINAL_STATES
_SHORTHAND_ALIASES = {
    "list": ["task", "list"],
}
_SHORTHAND_SUGGESTIONS = [
    "setup",
    "doctor",
    "init",
    "task list",
    "task status",
    "task report",
    "task logs",
    "worker run",
    "runner status",
]


def add_task_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=str(DEFAULT_BOARD_ROOT), help="AgentBC runtime record root.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbc")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Prepare AgentBC integrations.")
    setup_modes = setup.add_mutually_exclusive_group()
    setup_modes.add_argument("--show", action="store_true", help="Scan and display setup state without changing files.")
    setup_modes.add_argument("--update", action="store_true", help="Selectively update AgentBC setup artifacts.")
    setup_modes.add_argument("--clean", action="store_true", help="Selectively remove AgentBC-owned setup artifacts.")
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    setup.add_argument(
        "--permission-mode",
        choices=CANONICAL_PERMISSION_MODES,
        help="Set the default execution permission mode: inherit, safe, or full.",
    )

    doctor = sub.add_parser("doctor", help="Inspect build, configuration, and Runner identity without changing state.")
    doctor.add_argument("--json", action="store_true", help="Emit the stable machine-readable contract.")

    uninstall = sub.add_parser("uninstall", help="Remove AgentBC while choosing which managed data to keep.")
    uninstall.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    records = uninstall.add_mutually_exclusive_group()
    records.add_argument("--remove-records", dest="remove_records", action="store_true")
    records.add_argument("--keep-records", dest="remove_records", action="store_false")
    artifacts = uninstall.add_mutually_exclusive_group()
    artifacts.add_argument("--remove-artifacts", dest="remove_artifacts", action="store_true")
    artifacts.add_argument("--keep-artifacts", dest="remove_artifacts", action="store_false")
    uninstall.set_defaults(remove_records=None, remove_artifacts=None)

    init = sub.add_parser("init", help="Initialize the AgentBC runtime record directory.")
    add_task_root(init)

    record = sub.add_parser("record", help="Inspect or clean AgentBC task records.")
    record_sub = record.add_subparsers(dest="record_command", required=True)
    record_clean = record_sub.add_parser(
        "clean",
        help="Remove terminal-task reports and runtime diagnostics while preserving task state and indexes.",
    )
    add_task_root(record_clean)
    record_clean.add_argument("--dry-run", action="store_true", help="Show what would be removed without changing files.")

    task = sub.add_parser("task", help="Manage AgentBC task board tasks.")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create", help="Create a task from a steps.yaml file.")
    add_task_root(task_create)
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--assignee", required=True)
    task_create.add_argument("--steps", required=True, type=Path)
    task_create.add_argument(
        "--image",
        action="append",
        type=Path,
        default=[],
        help="Attach an existing image input. Repeat for multiple images when the executor supports it.",
    )
    task_create.add_argument("--session-id")
    task_create.add_argument("--source-platform")
    task_create.add_argument(
        "--permission-mode",
        choices=CANONICAL_PERMISSION_MODES,
        help="Override the configured permission mode for this task.",
    )
    task_create.add_argument(
        "--customer-dir",
        choices=["true", "false"],
        help=argparse.SUPPRESS,
    )
    task_create.add_argument(
        "--customer-path",
        default=DEFAULT_CUSTOMER_PATH,
        help='User supplied project directory, or "default path" for AgentBC managed workspace.',
    )
    task_create.add_argument(
        "--workspace",
        type=Path,
        help=argparse.SUPPRESS,
    )
    task_create.add_argument(
        "--output-dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    task_create.add_argument("--dispatch", action="store_true", help="Atomically create and submit the task to Runner.")
    task_create.add_argument("--config", type=Path)
    task_create.add_argument("--interval", type=float, default=2)
    task_create.add_argument(
        "--monitor",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Open a separate Terminal window for live task output after dispatch.",
    )

    task_list = task_sub.add_parser("list", help="List task board tasks.")
    add_task_root(task_list)
    task_list.add_argument("--status")
    task_list.add_argument("--assignee")
    task_list.add_argument("--current", action="store_true", help="Show only active task candidates.")
    task_list.add_argument("--all-iterations", action="store_true", help="Show every iteration instead of only chain heads.")
    task_list.add_argument("--watch", action=argparse.BooleanOptionalAction, default=None)
    task_list.add_argument("--interval", type=float, default=20.0)
    task_list.add_argument("--watch-task-id", help=argparse.SUPPRESS)
    task_list.add_argument("--auto-exit-when-idle", action="store_true", help=argparse.SUPPRESS)
    task_list.add_argument("--idle-grace", type=float, default=60.0, help=argparse.SUPPRESS)

    task_status = task_sub.add_parser("status", help="Show task status.")
    add_task_root(task_status)
    task_status.add_argument("id", nargs="?")
    task_status.add_argument("--json", action="store_true")
    task_status.add_argument("--watch", action="store_true")

    task_dispatch = task_sub.add_parser("dispatch", help="Submit an existing pending task through Runner gateway.")
    add_task_root(task_dispatch)
    task_dispatch.add_argument("id")
    task_dispatch.add_argument("--config", type=Path)
    task_dispatch.add_argument("--interval", type=float, default=2)
    task_dispatch.add_argument(
        "--monitor",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Open a separate Terminal window for live task output after dispatch.",
    )

    task_respond = task_sub.add_parser(
        "respond",
        help="Answer the current input request and resume the same task through Runner.",
    )
    add_task_root(task_respond)
    task_respond.add_argument("id")
    task_respond.add_argument("--input", required=True, dest="input_id")
    response = task_respond.add_mutually_exclusive_group(required=True)
    response.add_argument("--message")
    response.add_argument("--approve", action="store_true")
    response.add_argument("--deny", action="store_true")
    task_respond.add_argument("--config", type=Path)
    task_respond.add_argument("--interval", type=float, default=2)

    task_report = task_sub.add_parser("report", help="Generate and print a task report.")
    add_task_root(task_report)
    task_report.add_argument("id")
    task_report.add_argument("--format", choices=["markdown", "json"], default="markdown")

    task_logs = task_sub.add_parser("logs", help="Show live executor output for a task.")
    add_task_root(task_logs)
    task_logs.add_argument("id")
    task_logs.add_argument("--follow", action="store_true")
    task_logs.add_argument("--interval", type=float, default=1.0)

    task_progress = task_sub.add_parser("progress", help="Refresh task progress health for a running task.")
    add_task_root(task_progress)
    task_progress.add_argument("id")
    task_progress.add_argument("--state", default="running")
    task_progress.add_argument("--summary", required=True)
    task_progress.add_argument("--source", default="agent")

    task_pause = task_sub.add_parser("pause", help="Pause a task.")
    add_task_root(task_pause)
    task_pause.add_argument("id")
    task_pause.add_argument("--reason")

    task_resume = task_sub.add_parser("resume", help="Resume a paused task.")
    add_task_root(task_resume)
    task_resume.add_argument("id")

    task_cancel = task_sub.add_parser("cancel", help="Cancel a task.")
    add_task_root(task_cancel)
    task_cancel.add_argument("id")
    task_cancel.add_argument("--confirm", action="store_true")

    task_close = task_sub.add_parser("close", help="Close the current active task.")
    add_task_root(task_close)
    task_close.add_argument("id")
    task_close.add_argument("--confirm", action="store_true")

    task_correct = task_sub.add_parser("correct", help="Add a correction for a task step.")
    add_task_root(task_correct)
    task_correct.add_argument("id")
    task_correct.add_argument("--step", required=True, type=int)
    task_correct.add_argument("--message", required=True)

    task_retry = task_sub.add_parser("retry", help="Reset a task step to pending.")
    add_task_root(task_retry)
    task_retry.add_argument("id")
    task_retry.add_argument("--step", required=True, type=int)

    task_reassign = task_sub.add_parser("reassign", help="Reassign a task.")
    add_task_root(task_reassign)
    task_reassign.add_argument("id")
    task_reassign.add_argument("--to", required=True)

    task_handoff = task_sub.add_parser("handoff", help="Create a follow-up task for another agent from this task report.")
    add_task_root(task_handoff)
    task_handoff.add_argument("id")
    task_handoff.add_argument("--to", required=True)
    task_handoff.add_argument("--message")
    task_handoff.add_argument(
        "--image",
        action="append",
        type=Path,
        default=None,
        help="Replace inherited image inputs for this iteration. Repeat when supported.",
    )
    task_handoff.add_argument("--session-id")
    task_handoff.add_argument("--source-platform")
    task_handoff.add_argument(
        "--permission-mode",
        choices=CANONICAL_PERMISSION_MODES,
        help="Override the inherited permission mode for this handoff task.",
    )
    task_handoff.add_argument("--branch", action="store_true", help="Intentionally create a branch from a non-head task.")
    task_handoff.add_argument("--dispatch", action="store_true", help="Atomically create and submit the handoff task to Runner.")
    task_handoff.add_argument("--config", type=Path)
    task_handoff.add_argument("--interval", type=float, default=2)
    task_handoff.add_argument(
        "--monitor",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Open a separate Terminal window for live task output after dispatch.",
    )

    task_preflight = task_sub.add_parser("preflight", help="Validate a task before execution.")
    add_task_root(task_preflight)
    task_preflight.add_argument("id")

    task_recover = task_sub.add_parser("recover", help="Recover a task with an unhealthy run lease.")
    add_task_root(task_recover)
    task_recover.add_argument("id")
    task_recover.add_argument("--from-snapshot", action="store_true")

    task_callback = task_sub.add_parser(
        "callback",
        help="Record compatibility summary metadata; this does not replace the final marker.",
    )
    add_task_root(task_callback)
    task_callback.add_argument("id")
    task_callback.add_argument(
        "--state",
        required=True,
        choices=("completed", "input_required", "needs_recovery", "cancelled"),
        help="Agent-declared task completion state.",
    )
    task_callback.add_argument("--summary", required=True, help="Short completion or recovery summary.")
    task_callback.add_argument("--report-file", type=Path)
    task_callback.add_argument("--artifacts-dir", type=Path)
    task_callback.add_argument("--executor-run-id")
    task_callback.add_argument("--recovery-code", default="agent_reported_recovery")
    task_callback.add_argument("--spool", type=Path)
    task_callback.add_argument("--token", type=Path)

    worker = sub.add_parser("worker", help="Run a task board worker.")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)

    worker_run = worker_sub.add_parser("run", help="Claim and execute matching tasks.")
    add_task_root(worker_run)
    worker_run.add_argument("--executor", required=True)
    worker_run.add_argument("--once", action="store_true")
    worker_run.add_argument("--interval", type=float, default=2)
    worker_run.add_argument("--config", type=Path)
    worker_run.add_argument("--task-id")
    worker_run.add_argument("--detach", action="store_true")
    worker_run.add_argument("--runner-authorize", action="store_true", help=argparse.SUPPRESS)
    worker_run.add_argument(
        "--monitor",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Open a separate Terminal window for live task output.",
    )

    worker_reap = worker_sub.add_parser("reap", help="List orphaned runs for user-directed recovery.")
    add_task_root(worker_reap)

    runner = sub.add_parser("runner", help="Manage the local AgentBC execution Runner.")
    runner_sub = runner.add_subparsers(dest="runner_command", required=True)

    runner_start = runner_sub.add_parser("start", help="Start the local Runner in the background.")
    runner_start.add_argument("--spool", type=Path)
    runner_start.add_argument("--token", type=Path)
    runner_start.add_argument("--state-root", type=Path)
    runner_start.add_argument("--config", type=Path)
    runner_start.add_argument("--allow-root", type=Path, action="append", default=[])

    runner_stop = runner_sub.add_parser("stop", help="Stop the background AgentBC Runner.")
    runner_stop.add_argument("--spool", type=Path)

    runner_serve = runner_sub.add_parser("serve", help="Run the local file-spool execution service.")
    runner_serve.add_argument("--spool", type=Path)
    runner_serve.add_argument("--token", type=Path)
    runner_serve.add_argument("--state-root", type=Path)
    runner_serve.add_argument("--config", type=Path)
    runner_serve.add_argument("--allow-root", type=Path, action="append", default=[])
    runner_serve.add_argument("--hermes-command", type=Path)
    runner_serve.add_argument("--codex-command", type=Path)

    runner_status = runner_sub.add_parser("status", help="Check Runner health.")
    runner_status.add_argument("--spool", type=Path)
    runner_status.add_argument("--token", type=Path)

    runner_process_sample = runner_sub.add_parser(
        "process-sample",
        help="Sample AgentBC-related process pressure from inside Runner.",
    )
    runner_process_sample.add_argument("--spool", type=Path)
    runner_process_sample.add_argument("--token", type=Path)
    runner_process_sample.add_argument("--pattern", action="append", default=[])

    runner_cancel = runner_sub.add_parser("cancel", help="Cancel a Runner process.")
    runner_cancel.add_argument("run_id")
    runner_cancel.add_argument("--spool", type=Path)
    runner_cancel.add_argument("--token", type=Path)

    runner_show = runner_sub.add_parser("show", help="Show active Runner tasks or open one task monitor.")
    add_task_root(runner_show)
    runner_show.add_argument("id", nargs="?", help="Task id to open in a live-log Terminal.")
    runner_show.add_argument("--spool", type=Path)
    runner_show.add_argument("--token", type=Path)

    return parser


def command_task_create(args: argparse.Namespace) -> int:
    session_id, source_platform = _origin_context(args.session_id, args.source_platform)
    config_path = _optional_path_arg(getattr(args, "config", None))
    if getattr(args, "workspace", None) is not None or getattr(args, "output_dir", None) is not None:
        print('path_model_v2_required: use --customer-path "default path" or --customer-path <project-path> instead of --workspace/--output-dir')
        return 1
    try:
        customer_dir_hint = (
            _parse_customer_dir(args.customer_dir)
            if getattr(args, "customer_dir", None) is not None
            else None
        )
        customer_dir, customer_path = derive_customer_path_plan(
            getattr(args, "customer_path", DEFAULT_CUSTOMER_PATH),
            customer_dir_hint,
        )
    except ABCError as exc:
        print(f"task_create_error: {exc}")
        return 1
    if getattr(args, "dispatch", False) is True:
        from .runner import RunnerClient, RunnerError

        try:
            result = RunnerClient().create_and_dispatch(
                title=args.title,
                assignee=args.assignee,
                steps=load_steps(args.steps),
                board_root=args.root,
                config_path=config_path,
                session_id=session_id,
                source_platform=source_platform,
                customer_dir=customer_dir,
                customer_path=customer_path or DEFAULT_CUSTOMER_PATH,
                images=_image_args(args),
                interval_s=getattr(args, "interval", 2),
                monitor=getattr(args, "monitor", False),
                permission_mode=_permission_mode_arg(args),
            )
        except (ABCError, RunnerError) as exc:
            print(f"atomic_dispatch_error: {exc}")
            return 1
        _print_atomic_dispatch(result)
        return 0
    try:
        service = _task_service(args.root, config_path)
        task = service.create_task(
            title=args.title,
            assignee=args.assignee,
            steps=load_steps(args.steps),
            session_id=session_id,
            source_platform=source_platform,
            customer_dir=customer_dir,
            customer_path=customer_path,
            images=_image_args(args),
            permission_mode=_permission_mode_arg(args),
        )
    except ABCError as exc:
        print(f"task_create_error: {exc}")
        return 1

    print(f"created: {task.id}")
    if task.workspace:
        internal_task_dir = task.workspace.get("internal_task_dir")
        if internal_task_dir:
            print(f"task_record: {Path(internal_task_dir) / 'task.json'}")
        print(f"task_code: {task.workspace.get('task_code', '')}")
        print(f"iteration: {task.workspace.get('iteration', '')}")
        print(f"project_root: {task.workspace.get('project_root', '')}")
        print(f"requirements: {task.workspace.get('task_file', '')}")
        print(f"artifact_root: {task.workspace.get('artifact_root', task.workspace.get('artifacts_dir', ''))}")
        print(f"report: {task.workspace.get('report_file', '')}")
    if task.session_id:
        print(f"conversation: {source_platform}:{task.session_id}")
    return 0


def _origin_context(
    session_id: str | None,
    source_platform: str | None,
) -> tuple[str | None, str]:
    session_variables = (
        ("CODEX_THREAD_ID", "codex"),
        ("HERMES_SESSION_ID", "hermes"),
        ("CLAUDE_SESSION_ID", "claude"),
        ("OPENCODE_SESSION_ID", "opencode"),
    )
    explicit_platform = source_platform if isinstance(source_platform, str) else ""
    platform = explicit_platform.strip() or _detected_source_platform(
        session_variables
    )
    if isinstance(session_id, str):
        return session_id, platform
    matching_variable = next(
        (variable for variable, candidate in session_variables if candidate == platform),
        None,
    )
    trusted_session_id = (
        os.environ.get(matching_variable, "").strip() if matching_variable else ""
    )
    return trusted_session_id or None, platform


def _detected_source_platform(
    session_variables: tuple[tuple[str, str], ...],
) -> str:
    platform_markers = (
        ("CLAUDECODE", "claude"),
        ("CLAUDE_CODE_ENTRYPOINT", "claude"),
        ("CODEX_SHELL", "codex"),
        ("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex"),
    )
    for variable, platform in platform_markers:
        if os.environ.get(variable, "").strip():
            return platform
    bundle_identifier = os.environ.get("__CFBundleIdentifier", "").strip().lower()
    if "claude" in bundle_identifier or "anthropic" in bundle_identifier:
        return "claude"
    if "codex" in bundle_identifier or "openai" in bundle_identifier:
        return "codex"
    for variable, platform in session_variables:
        if os.environ.get(variable, "").strip():
            return platform
    return "cli"


def _parse_customer_dir(value: str) -> bool:
    return str(value).strip().lower() == "true"


def command_task_list(args: argparse.Namespace) -> int:
    raw_watch = getattr(args, "watch", None)
    watch = raw_watch if isinstance(raw_watch, bool) else bool(sys.stdout.isatty())
    if not watch:
        summaries = _task_list_summaries(args)
        _print_task_list_summaries(summaries, color=sys.stdout.isatty())
        return 0

    from .task_health import (
        dashboard_refresh_mtime,
        mark_dashboard_active,
        mark_dashboard_closed,
    )

    board_root = Path(args.root)
    raw_watch_task_id = getattr(args, "watch_task_id", "")
    watch_task_id = raw_watch_task_id.strip() if isinstance(raw_watch_task_id, str) else ""
    idle_started_at: float | None = None
    last_refresh_mtime = dashboard_refresh_mtime(board_root)
    try:
        while True:
            mark_dashboard_active(board_root)
            summaries = _task_list_summaries(args)
            _clear_screen()
            print(f"AgentBC Task List  refresh={int(max(args.interval, 1))}s  Ctrl+C to exit")
            print(f"Board: {Path(args.root).expanduser().resolve()}")
            print()
            _print_task_list_summaries(summaries, color=sys.stdout.isatty())
            sys.stdout.flush()
            has_active = any(_summary_keeps_dashboard_alive(summary, watch_task_id) for summary in summaries)
            if getattr(args, "auto_exit_when_idle", False):
                if has_active:
                    idle_started_at = None
                else:
                    idle_started_at = idle_started_at or time.monotonic()
                    if time.monotonic() - idle_started_at >= max(float(args.idle_grace), 0.0):
                        return 0
            deadline = time.monotonic() + max(float(args.interval), 1.0)
            while time.monotonic() < deadline:
                time.sleep(1.0)
                current_refresh_mtime = dashboard_refresh_mtime(board_root)
                if current_refresh_mtime != last_refresh_mtime:
                    last_refresh_mtime = current_refresh_mtime
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        mark_dashboard_closed(board_root)


def _summary_keeps_dashboard_alive(summary: dict[str, Any], watch_task_id: str = "") -> bool:
    status = str(summary.get("status") or "")
    return status not in _TASK_TERMINAL_STATUSES


def _task_list_summaries(args: argparse.Namespace) -> list[dict[str, Any]]:
    service = _task_service(args.root)
    raw_watch_task_id = getattr(args, "watch_task_id", "")
    watch_task_id = raw_watch_task_id.strip() if isinstance(raw_watch_task_id, str) else ""
    if watch_task_id:
        from .task_health import dashboard_cohort_exists, dashboard_task_ids

        summaries = service.list_task_summaries(
            status=args.status,
            assignee=args.assignee,
            current_only=False,
            all_iterations=True,
        )
        summary_by_id = {
            str(summary.get("task_id") or "").upper(): summary
            for summary in summaries
        }
        cohort_ids = (
            dashboard_task_ids(args.root)
            if dashboard_cohort_exists(args.root)
            else [watch_task_id.upper()]
        )
        return [summary_by_id[task_id] for task_id in cohort_ids if task_id in summary_by_id]
    return service.list_task_summaries(
        status=args.status,
        assignee=args.assignee,
        current_only=getattr(args, "current", False),
        all_iterations=getattr(args, "all_iterations", False),
    )


def _print_task_list_summaries(summaries: list[dict[str, Any]], *, color: bool = False) -> None:
    timer_now = _utc_now_cli()
    if summaries:
        print("task_id\titer\troute\ttimer\ttitle")
    for summary in summaries:
        print(_format_task_candidate(summary, color=color, timer_now=timer_now))


def command_task_progress(args: argparse.Namespace) -> int:
    from .task_health import write_task_progress

    service = _task_service(args.root)
    try:
        task = service.get_task(args.id)
        payload = write_task_progress(
            task,
            state=str(args.state or "running"),
            message=str(args.summary or ""),
            source=str(args.source or "agent"),
        )
    except ABCError as exc:
        print(f"{exc.code}: {exc}")
        return 1
    except OSError as exc:
        print(f"progress_error: {exc}")
        return 1
    print(f"progress: {payload['task_id']}")
    print(f"state: {payload['state']}")
    print(f"updated_at: {payload['updated_at']}")
    return 0


def command_task_status(args: argparse.Namespace) -> int:
    service = _task_service(args.root)
    try:
        if args.watch:
            resolution = service.resolve_task(args.id)
            if resolution.get("resolved_task_id") is None:
                _print_resolution_payload(resolution, as_json=args.json)
                return 1
            resolved_task_id = str(resolution["resolved_task_id"])
            while True:
                try:
                    from .run_lease import reconcile_task

                    reconcile_task(resolved_task_id, service.board_root)
                except PermissionError:
                    pass
                status = _decorate_task_status(service.get_task(resolved_task_id), service.board_root)
                payload = {
                    **resolution,
                    "resolved_task_id": resolved_task_id,
                    "current_task": status,
                }
                _print_resolution_payload(payload, as_json=args.json)
                if status["status"] in _TASK_TERMINAL_STATUSES:
                    return 0
                time.sleep(2)
        resolution = service.resolve_task(args.id)
    except ABCError as exc:
        if exc.code != "task_not_found":
            raise
        print(f"task_not_found: {exc}")
        return 1

    resolved_task_id = resolution.get("resolved_task_id")
    if resolved_task_id is not None:
        try:
            from .run_lease import reconcile_task

            reconcile_task(str(resolved_task_id), service.board_root)
        except PermissionError:
            pass
        status = _decorate_task_status(service.get_task(str(resolved_task_id)), service.board_root)
        resolution = {
            **resolution,
            "resolved_task_id": str(resolved_task_id),
            "current_task": status,
        }

    _print_resolution_payload(resolution, as_json=args.json)
    return 0


def command_task_logs(args: argparse.Namespace) -> int:
    service = _task_service(args.root)
    stdout_offset = 0
    stderr_offset = 0
    last_status_line = ""
    last_status_print_at = 0.0
    while True:
        status = task_to_status(service.get_task(args.id))
        execution = _execution_snapshot(args.id, service.board_root, status)
        remote, run_id, source, error = _task_log_remote(execution)
        if remote is None:
            if not args.follow or status["status"] in _TASK_TERMINAL_STATUSES:
                detail = error or f"no run has started for {args.id}"
                print(f"logs_unavailable: {detail}")
                return 1
            status_line = _task_log_status_line(status, execution, None, "waiting", None)
            now = time.monotonic()
            if status_line != last_status_line or now - last_status_print_at >= 5:
                print(status_line, flush=True)
                last_status_line = status_line
                last_status_print_at = now
            time.sleep(max(args.interval, 0.1))
            continue
        stdout = str(remote.get("stdout") or "")
        stderr = str(remote.get("stderr") or "")
        if len(stdout) > stdout_offset:
            print(stdout[stdout_offset:], end="", flush=True)
            stdout_offset = len(stdout)
        if len(stderr) > stderr_offset:
            print(stderr[stderr_offset:], end="", file=sys.stderr, flush=True)
            stderr_offset = len(stderr)
        now = time.monotonic()
        status_line = _task_log_status_line(status, execution, run_id, source, remote)
        if (
            (len(stdout) == stdout_offset and len(stderr) == stderr_offset)
            and (status_line != last_status_line or now - last_status_print_at >= 5)
        ):
            print(status_line, flush=True)
            last_status_line = status_line
            last_status_print_at = now
        if not args.follow or remote.get("status") in {"completed", "failed", "cancelled"}:
            return 0
        time.sleep(max(args.interval, 0.1))


def command_task_dispatch(args: argparse.Namespace) -> int:
    from .runner import RunnerClient, RunnerError

    try:
        result = RunnerClient().dispatch_task(
            args.id,
            args.root,
            _optional_path_arg(getattr(args, "config", None)),
            getattr(args, "interval", 2),
            getattr(args, "monitor", False),
        )
    except (ABCError, RunnerError) as exc:
        print(f"dispatch_error: {exc}")
        return 1
    print(f"dispatched: {args.id}")
    if result.get("task_id") and result.get("task_id") != args.id:
        print(f"resolved_task: {result.get('task_id')}")
    print(f"worker_run_id: {result.get('run_id', '')}")
    print(f"status: {result.get('dispatch_status', '')}")
    print(f"monitor: {result.get('monitor_status', 'not_requested')}")
    return 0


def command_task_respond(args: argparse.Namespace) -> int:
    from .runner import RunnerClient, RunnerError

    if args.message is not None:
        response_type, message = "message", str(args.message)
    elif args.approve:
        response_type, message = "approve", ""
    else:
        response_type, message = "deny", ""
    try:
        result = RunnerClient().respond_task(
            args.id,
            args.input_id,
            response_type,
            message,
            args.root,
            _optional_path_arg(getattr(args, "config", None)),
            getattr(args, "interval", 2),
        )
    except (ABCError, RunnerError) as exc:
        print(f"respond_error: {exc}")
        return 1
    print(f"response: {result.get('status', '')}")
    print(f"task_id: {result.get('task_id', args.id)}")
    print(f"input_id: {result.get('input_id', args.input_id)}")
    if result.get("dispatch_required"):
        print(f"worker_run_id: {result.get('run_id', '')}")
        print(f"same_task: {'yes' if result.get('same_task') else 'no'}")
    return 0


def _execution_snapshot(task_id: str, board_root: Path, status: dict) -> dict:
    from .run_lease import load_lease
    from .runner import RunnerClient, RunnerError

    extensions = status.get("extensions") or {}
    execution = dict(extensions.get("agentbc.execution") or {})
    lease = load_lease(task_id, Path(board_root))
    # OBS-001: the current lease state always comes from the authoritative
    # run_lease.json. Stale extension snapshots are historical evidence only.
    if lease is not None:
        execution.update(
            {
                "executor_run_id": lease.run_id,
                "executor_id": lease.executor_id,
                "lease_state": lease.state,
                "started_at": lease.started_at,
                "last_heartbeat_at": lease.last_heartbeat_at,
            }
        )
    else:
        execution["lease_state"] = "closed"
    run_id = execution.get("executor_run_id") or execution.get("worker_run_id")
    if run_id:
        try:
            remote = RunnerClient().status(str(run_id))
        except RunnerError as exc:
            execution["runner_status"] = "unavailable"
            execution["runner_error"] = str(exc)
        else:
            execution.update(
                {
                    "runner_status": remote.get("status"),
                    "pid": remote.get("pid"),
                    "output_truncated": remote.get("output_truncated", False),
                }
            )
    return execution


def _decorate_task_status(task, board_root: Path) -> dict:
    from .timing_view import build_timing_view

    status = task_to_status(task)
    try:
        chain = TaskService(board_root).resolve_chain(task.id).to_dict()
    except ABCError:
        chain = {}
    lineage = ((status.get("extensions") or {}).get("agentbc.lineage") or {})
    if chain:
        status["chain_root_task_id"] = chain.get("chain_root_task_id")
        status["head_task_ids"] = chain.get("head_task_ids", [])
        status["is_chain_head"] = chain.get("requested_is_head")
        status["chain_anomalies"] = chain.get("anomalies", [])
    status["parent_task_id"] = lineage.get("parent_task_id")
    status["base_task_id"] = lineage.get("base_task_id") or task.id
    status["iteration_index"] = lineage.get("iteration_index", 1)
    status["branch_mode"] = lineage.get("branch_mode", "linear")
    status["task_code"] = lineage.get("task_code") or ((status.get("workspace") or {}).get("task_code"))
    status["iteration"] = ((status.get("workspace") or {}).get("iteration")) or f"{int(status.get('iteration_index', 1)):03d}"
    status["task_date"] = lineage.get("task_date") or ((status.get("workspace") or {}).get("task_date"))
    status["chain_task_id"] = lineage.get("chain_task_id") or ((status.get("workspace") or {}).get("chain_task_id"))
    execution = _execution_snapshot(task.id, board_root, status)
    timing = build_timing_view(task, board_root)
    execution["lease_state"] = timing["lease_state"]
    report_file = str(((status.get("workspace") or {}).get("report_file")) or "")
    report_ready = bool(report_file) and Path(report_file).expanduser().exists()
    final_callback = ((status.get("extensions") or {}).get("agentbc.final_callback") or {})
    status["has_final_callback"] = bool(final_callback)
    status["report_ready"] = report_ready
    status["run_lease_state"] = timing["lease_state"]
    status["execution"] = execution
    status["timing"] = timing
    status["debug"] = {
        "execution": execution,
        "timing": timing,
        "permission": (status.get("extensions") or {}).get(PERMISSION_EXTENSION_KEY) or {},
    }
    return status


def _task_log_remote(execution: dict) -> tuple[dict | None, str | None, str | None, str | None]:
    from .runner import RunnerClient, RunnerError

    candidates: list[tuple[str, str]] = []
    executor_run_id = execution.get("executor_run_id")
    worker_run_id = execution.get("worker_run_id")
    if isinstance(executor_run_id, str) and executor_run_id:
        candidates.append((executor_run_id, "executor"))
    if (
        isinstance(worker_run_id, str)
        and worker_run_id
        and worker_run_id != executor_run_id
    ):
        candidates.append((worker_run_id, "worker"))
    if not candidates:
        return None, None, None, None

    client = RunnerClient()
    last_error = ""
    for run_id, source in candidates:
        try:
            return client.status(run_id), run_id, source, None
        except RunnerError as exc:
            last_error = str(exc)
    return None, None, None, last_error or "runner log stream unavailable"


def _task_log_status_line(
    status: dict,
    execution: dict,
    run_id: str | None,
    source: str,
    remote: dict | None,
) -> str:
    task_status = status.get("status", "unknown")
    runner_status = (
        str(remote.get("status"))
        if isinstance(remote, dict) and remote.get("status") is not None
        else str(execution.get("runner_status", "pending"))
    )
    lease_state = execution.get("lease_state", "unknown")
    selected_run = run_id or "pending"
    return (
        f"[agentbc] task={status.get('id', '')} "
        f"task_status={task_status} lease={lease_state} "
        f"log_source={source} runner={runner_status} run={selected_run}"
    )


def command_task_intervention(args: argparse.Namespace) -> int:
    config_path = _optional_path_arg(getattr(args, "config", None))
    session_id, source_platform = _origin_context(
        getattr(args, "session_id", None),
        getattr(args, "source_platform", None),
    )
    if args.task_command == "handoff" and getattr(args, "dispatch", False) is True:
        from .runner import RunnerClient, RunnerError

        try:
            result = RunnerClient().handoff_and_dispatch(
                source_task_id=args.id,
                target_assignee=args.to,
                message=args.message,
                branch=getattr(args, "branch", False),
                board_root=args.root,
                config_path=config_path,
                interval_s=getattr(args, "interval", 2),
                monitor=getattr(args, "monitor", False),
                session_id=session_id,
                source_platform=source_platform,
                images=_image_args(args, inherit_when_missing=True),
                permission_mode=_permission_mode_arg(args),
            )
        except RunnerError as exc:
            print(f"atomic_dispatch_error: {exc}")
            return 1
        print(f"handoff_created: {args.id} -> {result['task_id']}")
        _print_atomic_dispatch(result)
        return 0
    service = _task_service(args.root, config_path)
    try:
        if args.task_command == "pause":
            service.pause_task(args.id, reason=args.reason)
        elif args.task_command == "resume":
            service.resume_task(args.id)
        elif args.task_command == "cancel":
            if not args.confirm:
                print("cancel_requires_confirmation: pass --confirm")
                return 1
            task = service.get_task(args.id)
            service.cancel_task(task.id)
            cancellation_errors = _cancel_task_runner_runs(task)
            _finish_task_close_cleanup(task, service.board_root)
        elif args.task_command == "close":
            plan = service.plan_task_close(args.id)
            if plan["is_chain_iteration"] and not args.confirm:
                if not _confirm_chain_close(plan):
                    print("close_cancelled")
                    return 0
            reservation = service.reserve_task_close(
                args.id,
                confirmed=bool(args.confirm or plan["is_chain_iteration"]),
            )
            task = reservation["task"]
            cancellation_errors = _cancel_task_runner_runs(task)
            if cancellation_errors:
                service.abort_task_close(reservation["task_id"], reservation["close_token"])
                for error in cancellation_errors:
                    print(f"execution_cancel_warning: {error}")
                return 1
            result = service.commit_task_close(reservation["task_id"], reservation["close_token"])
            _finish_task_chain_close_cleanup([result["task_id"]], service.board_root)
            print(f"close: {result['task_id']}")
            return 0
        elif args.task_command == "correct":
            service.correct_step(args.id, args.step, args.message)
        elif args.task_command == "retry":
            service.retry_step(args.id, args.step)
        elif args.task_command == "reassign":
            service.reassign_task(args.id, args.to)
        elif args.task_command == "handoff":
            task = service.handoff_task(
                args.id,
                args.to,
                args.message,
                branch=getattr(args, "branch", False),
                session_id=session_id,
                source_platform=source_platform,
                images=_image_args(args, inherit_when_missing=True),
                permission_mode=_permission_mode_arg(args),
            )
            print(f"handoff_created: {args.id} -> {task.id}")
            print(f"assignee: {task.assignee}")
            if task.workspace:
                print(f"task_code: {task.workspace.get('task_code', '')}")
                print(f"iteration: {task.workspace.get('iteration', '')}")
                print(f"project_root: {task.workspace.get('project_root', '')}")
                print(f"requirements: {task.workspace.get('task_file', '')}")
                print(f"artifact_root: {task.workspace.get('artifact_root', task.workspace.get('artifacts_dir', ''))}")
                print(f"report: {task.workspace.get('report_file', '')}")
            return 0
        else:
            raise AssertionError(args.task_command)
    except ABCError as exc:
        print(f"{exc.code}: {exc}")
        return 1
    if args.task_command in {"cancel", "close"} and cancellation_errors:
        for error in cancellation_errors:
            print(f"execution_cancel_warning: {error}")
        return 1
    print(f"{args.task_command}: {args.id}")
    return 0


def _cancel_task_runner_runs(task: Any) -> list[str]:
    from .runner import RunnerClient, RunnerError

    execution = dict(((getattr(task, "extensions", None) or {}).get("agentbc.execution") or {}))
    candidates = (
        ("executor", str(execution.get("executor_run_id") or "")),
        ("worker", str(execution.get("worker_run_id") or "")),
    )
    client = RunnerClient()
    errors: list[str] = []
    seen: set[str] = set()
    for source, run_id in candidates:
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        try:
            client.cancel(run_id)
        except RunnerError as exc:
            message = str(exc)
            if "unknown runner run" in message:
                continue
            errors.append(f"{source} run {run_id}: {message}")
    return errors


def _confirm_chain_close(plan: dict[str, Any]) -> bool:
    print(f"This will delete {plan['task_id']} record and report.")
    print("Original project files may already have changed and cannot be restored.")
    try:
        answer = input("Continue? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _image_args(
    args: argparse.Namespace,
    *,
    inherit_when_missing: bool = False,
) -> list[Path] | None:
    value = getattr(args, "image", None)
    if value is None:
        return None if inherit_when_missing else []
    if isinstance(value, (list, tuple)):
        return [Path(image).expanduser() for image in value]
    return None if inherit_when_missing else []


def _permission_mode_arg(args: argparse.Namespace) -> str | None:
    value = getattr(args, "permission_mode", None)
    return value if isinstance(value, str) else None


def _finish_task_close_cleanup(task: Any, board_root: str | Path) -> None:
    from .task_health import (
        cleanup_cancelled_task_files,
        remove_dashboard_task,
        request_dashboard_refresh,
    )

    cleanup_cancelled_task_files(task)
    remove_dashboard_task(board_root, task.id)
    request_dashboard_refresh(board_root)


def _finish_task_chain_close_cleanup(task_ids: list[str], board_root: str | Path) -> None:
    from .task_health import remove_dashboard_task, request_dashboard_refresh

    for task_id in task_ids:
        remove_dashboard_task(board_root, task_id)
    request_dashboard_refresh(board_root)


def command_task_preflight(args: argparse.Namespace) -> int:
    result = _task_service(args.root).preflight(args.id)
    print(json.dumps({"ok": result.ok, "errors": result.errors}, indent=2))
    return 0 if result.ok else 1


def command_task_report(args: argparse.Namespace) -> int:
    from .reports import generate_report, generate_report_md

    try:
        report = generate_report(args.id, Path(args.root))
        markdown = generate_report_md(args.id, Path(args.root))
    except ABCError as exc:
        print(f"{exc.code}: {exc}")
        return 1
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(markdown, end="")
    return 0


def command_task_recover(args: argparse.Namespace) -> int:
    from .run_lease import recover_task

    try:
        result = recover_task(args.id, Path(args.root), from_snapshot=args.from_snapshot)
    except ABCError as exc:
        print(f"{exc.code}: {exc}")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_task_callback(args: argparse.Namespace) -> int:
    from .runner import RunnerClient, RunnerError

    try:
        result = RunnerClient(args.spool, args.token).agent_callback(
            args.id,
            args.root,
            args.state,
            args.summary,
            report_file=args.report_file,
            artifacts_dir=args.artifacts_dir,
            executor_run_id=args.executor_run_id,
            recovery_code=args.recovery_code,
        )
    except RunnerError as exc:
        print(f"callback_error: {exc}")
        return 1
    print(f"callback: {result.get('task_id', args.id)}")
    print(f"status: {result.get('status', '')}")
    print(f"event: {result.get('event_type', '')}")
    if result.get("report_file"):
        print(f"report: {result.get('report_file')}")
    return 0


def command_worker_reap(args: argparse.Namespace) -> int:
    from .run_lease import reap_orphaned

    orphaned = reap_orphaned(Path(args.root))
    if not orphaned:
        print("no orphaned tasks")
        return 0
    print(json.dumps(orphaned, indent=2, ensure_ascii=False))
    return 0


def _is_explicit_retryable_failure(failure: Any) -> bool:
    if not isinstance(failure, dict) or failure.get("retryable") is not True:
        return False
    kind = str(failure.get("kind") or "").lower()
    if "cancel" in kind or str(failure.get("layer") or "") == "flow_contract":
        return False
    return any(
        marker in kind
        for marker in (
            "transport",
            "infrastructure",
            "connection",
            "timeout",
            "runner_status",
            "runner_unavailable",
            "api_",
        )
    )


def command_worker_run(args: argparse.Namespace) -> int:
    from .run_lease import reconcile_task
    if getattr(args, "detach", False) is True:
        from .runner import RunnerClient, RunnerError

        raw_task_id = getattr(args, "task_id", None)
        task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else None
        if not task_id:
            print("worker_error: --detach requires --task-id")
            return 1
        try:
            raw_monitor = getattr(args, "monitor", False)
            monitor = raw_monitor if isinstance(raw_monitor, bool) else False
            dispatched = RunnerClient().dispatch_worker(
                task_id,
                args.executor,
                args.root,
                args.config,
                args.interval,
                monitor,
            )
        except RunnerError as exc:
            service = TaskService(args.root, config=load_config(args.config))
            try:
                recovery_marked = service.mark_task_needs_recovery(
                    task_id,
                    "runner_dispatch_failed",
                    str(exc),
                    {"executor": args.executor},
                )
                if recovery_marked:
                    _write_terminal_report(task_id, service.board_root)
                _request_task_list_refresh(service.board_root)
            except ABCError:
                pass
            print(f"worker_error: async dispatch failed: {exc}")
            return 1
        print(f"dispatched: {task_id}")
        print(f"worker_run_id: {dispatched['run_id']}")
        print(f"status: {dispatched['dispatch_status']}")
        print(f"monitor: {dispatched.get('monitor_status', 'not_requested')}")
        return 0

    config = load_config(args.config)
    service = TaskService(args.root, config=config)
    try:
        executor = get_executor(
            args.executor,
            get_executor_config(config, args.executor),
        )
    except (TypeError, ValueError) as exc:
        print(f"worker_error: {exc}")
        return 1

    probe = executor.probe()
    if not probe.ok:
        raw_task_id = getattr(args, "task_id", None)
        requested_task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else None
        if requested_task_id:
            try:
                requested = service.get_task(requested_task_id)
                if requested.status == "pending" and requested.assignee == args.executor:
                    recovery_marked = service.mark_task_needs_recovery(
                        requested.id,
                        "executor_probe_failed",
                        probe.message,
                        {"executor": args.executor, "probe": probe.details},
                    )
                    if recovery_marked:
                        _write_terminal_report(requested.id, service.board_root)
                        _notify_terminal(
                            service,
                            requested.id,
                            "task.recovery_required",
                            "warning",
                            probe.message,
                        )
                    _request_task_list_refresh(service.board_root)
            except ABCError:
                pass
        print(f"worker_error: executor probe failed: {probe.message}")
        return 1

    while True:
        for active_status in ("running", "assigned", "working"):
            for active_task in service.list_tasks(status=active_status, assignee=args.executor):
                reconcile_task(active_task.id, service.board_root)
        raw_task_id = getattr(args, "task_id", None)
        requested_task_id = raw_task_id if isinstance(raw_task_id, str) and raw_task_id else None
        if requested_task_id:
            requested = service.get_task(requested_task_id)
            requested_execution = dict((requested.extensions or {}).get("agentbc.execution") or {})
            requested_is_resuming = (
                requested.status == "running"
                and requested_execution.get("internal_status") == "resuming"
            )
            pending = (
                [requested]
                if (requested.status == "pending" or requested_is_resuming)
                and requested.assignee == args.executor
                else []
            )
        else:
            pending = service.list_tasks(status="pending", assignee=args.executor)
        if not pending:
            if args.once:
                return 0
            time.sleep(max(args.interval, 0.1))
            continue

        task = min(pending, key=lambda item: (item.created_at, item.id))
        executor_started = False
        try:
            service.start_task_run(task.id, args.executor)
            claimed_task = service.get_task(task.id)
            start = executor.start(
                {
                    "task_id": claimed_task.id,
                    "title": claimed_task.title,
                    "steps": claimed_task.steps,
                    "workspace": _task_workspace(claimed_task, service.board_root, service.config),
                    "task_board": {"root": str(service.board_root)},
                    "extensions": claimed_task.extensions,
                    "runner_authorization_required": (
                        getattr(args, "runner_authorize", False) is True
                    ),
                }
            )
            if not start.ok:
                start_code = (
                    "input_resume_start_failed"
                    if requested_task_id and requested_is_resuming
                    else "executor_start_failed"
                )
                recovery_marked = service.mark_task_needs_recovery(
                    task.id,
                    start_code,
                    start.message,
                    {
                        "executor": args.executor,
                        "phase": "resume_start" if start_code == "input_resume_start_failed" else "start",
                    },
                )
                if recovery_marked:
                    _write_terminal_report(task.id, service.board_root)
                    _notify_terminal(service, task.id, "task.recovery_required", "warning", start.message)
                _request_task_list_refresh(service.board_root)
                print(f"worker_error: executor start failed for {task.id}: {start.message}")
                return 1
            executor_started = True
            service.update_execution_metadata(task.id, {"executor_run_id": start.run_id})

            while True:
                poll = executor.poll(start.run_id)
                if poll.status in ("completed", "cancelled", "input_required", "needs_recovery", "failed", "needs_review"):
                    break
                time.sleep(max(args.interval, 0.1))

            if poll.status not in {"completed", "input_required", "cancelled"}:
                failure = poll.result.get("failure")
                failure_message = (
                    failure.get("message")
                    if isinstance(failure, dict) and failure.get("message")
                    else f"Agent execution failed ({poll.status})"
                )
                failure_code = (
                    str(failure.get("kind"))
                    if isinstance(failure, dict) and failure.get("kind")
                    else "executor_terminal_failure"
                )
                details = {"executor": args.executor, "result": poll.result, "progress": poll.progress}
                if _is_explicit_retryable_failure(failure):
                    terminal_marked = service.mark_task_needs_recovery(
                        task.id, failure_code, failure_message, details
                    )
                    event_type, level = "task.recovery_required", "warning"
                else:
                    terminal_marked = service.mark_task_failed(
                        task.id, failure_code, failure_message, details
                    )
                    event_type, level = "task.failed", "error"
                if terminal_marked:
                    _write_terminal_report(task.id, service.board_root)
                    _notify_terminal(service, task.id, event_type, level, failure_message)
                _request_task_list_refresh(service.board_root)
                print(f"worker_error: executor failed for {task.id}: {failure_message}")
                return 1

            callback = poll.result.get("agent_callback")
            exit_code = poll.result.get("returncode")
            if not isinstance(exit_code, int):
                exit_code = 0
            summary = str(poll.result.get("summary") or "Executor exited normally")
            finalized_from_worker = service.finalize_task_from_executor_exit(
                task.id,
                executor_run_id=start.run_id,
                summary=summary,
                exit_code=exit_code,
                callback=callback if isinstance(callback, dict) else None,
            )
            finalized = service.get_task(task.id)
            final_status = finalized.status
            if final_status == "completed":
                event_type, level = "task.finalized", "done"
            elif final_status == "input_required":
                event_type, level = "task.input_required", "input"
            elif final_status == "cancelled":
                event_type, level = "task.finalized", "info"
            elif final_status == "needs_recovery":
                event_type, level = "task.recovery_required", "warning"
            else:
                event_type, level = "task.failed", "error"
            if finalized_from_worker:
                if final_status == "input_required":
                    _notify_input_required(
                        service,
                        task.id,
                        config_path=getattr(args, "config", None),
                        interval_s=getattr(args, "interval", 2),
                    )
                else:
                    _notify_terminal(service, task.id, event_type, level, summary)
            _request_task_list_refresh(service.board_root)
            print(f"{final_status}: {task.id}")
        except ABCError as exc:
            try:
                terminal_marked = (
                    service.mark_task_failed(
                        task.id,
                        exc.code,
                        str(exc),
                        {"executor": args.executor, "details": exc.details},
                    )
                    if executor_started
                    else service.mark_task_needs_recovery(
                        task.id,
                        exc.code,
                        str(exc),
                        {"executor": args.executor, "details": exc.details},
                    )
                )
                if terminal_marked:
                    _write_terminal_report(task.id, service.board_root)
                    event_type = "task.failed" if executor_started else "task.recovery_required"
                    level = "error" if executor_started else "warning"
                    _notify_terminal(service, task.id, event_type, level, str(exc))
                _request_task_list_refresh(service.board_root)
            except ABCError:
                pass
            print(f"worker_error: {exc}")
            return 1

        if args.once:
            return 0


def command_runner(args: argparse.Namespace) -> int:
    from .runner import (
        RunnerClient,
        RunnerError,
        create_runner_service,
        start_runner_background,
        stop_runner_background,
    )

    if args.runner_command == "start":
        result = start_runner_background(
            config_path=args.config,
            spool_root=args.spool,
            token_path=args.token,
            state_root=args.state_root,
            extra_roots=args.allow_root,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.runner_command == "stop":
        result = stop_runner_background(spool_root=args.spool)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.runner_command == "serve":
        existing = _probe_existing_runner(args.spool, args.token)
        if existing is not None:
            print(
                f"runner_error: runner already running for spool "
                f"{Path(args.spool).expanduser() if args.spool else 'default'}: "
                f"pid {existing.get('pid', 'unknown')}"
            )
            return 1
        config = load_config(args.config)
        roots = resolve_runner_allowed_roots(config, args.allow_root)
        try:
            service = create_runner_service(
                spool_root=args.spool,
                token_path=args.token,
                state_root=args.state_root,
                allowed_roots=roots,
                hermes_command=args.hermes_command,
                codex_command=args.codex_command,
                config=config,
            )
        except (OSError, RunnerError) as exc:
            print(f"runner_error: {exc}")
            return 1
        print(
            f"runner_ready: spool={service.spool_root} "
            f"allowed_roots={','.join(str(Path(root).expanduser().resolve()) for root in roots)}",
            flush=True,
        )
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: service.shutdown())
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            service.shutdown()
            signal.signal(signal.SIGTERM, previous_sigterm)
        return 0

    client = RunnerClient(spool_root=args.spool, token_path=args.token)
    if args.runner_command == "show":
        if not args.id:
            return _print_runner_show_candidates(args.root)
        try:
            result = client.show_task(args.id, args.root)
        except RunnerError as exc:
            print(f"runner_error: {exc}")
            return 1
        print(f"task: {result.get('task_id', args.id)}")
        print(f"monitor: {result.get('monitor_status', 'unknown')}")
        if result.get("monitor_message"):
            print(f"message: {result['monitor_message']}")
        return 0
    if args.runner_command == "process-sample":
        try:
            result = client.process_sample(args.pattern)
        except RunnerError as exc:
            print(f"runner_error: {exc}")
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    try:
        result = (
            client.health()
            if args.runner_command == "status"
            else client.cancel(args.run_id)
        )
    except RunnerError as exc:
        print(f"runner_error: {exc}")
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    from .doctor import build_doctor_report, render_doctor_text

    report = build_doctor_report()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_doctor_text(report))
    return 0 if report["ok"] else 1


def _probe_existing_runner(spool: Path | None, token: Path | None) -> dict[str, Any] | None:
    from .runner import RunnerClient, RunnerError

    try:
        health = RunnerClient(spool_root=spool, token_path=token, timeout_s=0.5).health()
    except RunnerError:
        return None
    if health.get("ok") and health.get("status") == "ready":
        return health
    return None


def _print_runner_show_candidates(board_root: Path) -> int:
    service = TaskService(board_root)
    tasks = [task for task in service.list_tasks() if task.status in {"running", "assigned", "working", "pause_pending", "paused", "in_progress"}]
    if not tasks:
        print("No active AgentBC tasks. Pass a task id to open a specific task monitor.")
        return 0
    print("Active AgentBC tasks:")
    print("task_id\tstatus\texecutor\tduration\ttitle\treport")
    now = _utc_now_cli()
    for task in tasks:
        workspace = task.workspace or {}
        print(
            "\t".join(
                [
                    task.id,
                    task.status,
                    task.assignee,
                    _format_elapsed(task.created_at, now),
                    _compact_notification_text(task.title, 80),
                    str(workspace.get("report_file") or ""),
                ]
            )
        )
    print("Open one with: agentbc runner show <task-id>")
    return 0


def command_shorthand(args: argparse.Namespace) -> int:
    description = args.description.strip()
    if _should_reject_shorthand(description):
        suggestion = _suggest_shorthand_command(description)
        message = (
            f"unknown command: {description}. "
            "Use `agentbc task list` to inspect tasks, "
            "`agentbc 4XMC` or `agentbc task status 4XMC-001` for task status, "
            "or pass a longer quoted description to create a task."
        )
        if suggestion:
            message = f"{message} Did you mean `{suggestion}`?"
        print(message)
        return 1
    service = _task_service(args.root)
    try:
        task = service.create_task(
            title=description,
            assignee="codex",
            steps=[{"id": 1, "description": description}],
            customer_dir=False,
        )
    except ABCError as exc:
        print(f"task_create_error: {exc}")
        return 1
    print(f"created: {task.id}")
    return command_worker_run(
        argparse.Namespace(
            root=args.root,
            executor="codex",
            once=True,
            interval=2,
            config=None,
        )
    )


def _print_task_status(status: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    print(f"{status['id']}  {status['title']}")
    print(f"Status: {status['status']}\tAssignee: {status['assignee']}")
    print(f"Updated: {status['updated_at']}")
    permission = (status.get("extensions") or {}).get(PERMISSION_EXTENSION_KEY) or {}
    if permission:
        print(
            "Permission: "
            f"requested={permission.get('requested_mode', '-')} "
            f"effective={permission.get('effective_mode', '-')} "
            f"source={permission.get('selection_source', '-')}"
        )
    if status.get("chain_root_task_id"):
        heads = ", ".join(status.get("head_task_ids") or []) or "-"
        print(
            f"Task: code={status.get('task_code') or '-'} "
            f"iteration={status.get('iteration') or status.get('iteration_index', 1)} "
            f"parent={status.get('parent_task_id') or '-'} "
            f"head={'yes' if status.get('is_chain_head') else 'no'} heads={heads}"
        )
    print(
        f"Final callback: {'yes' if status.get('has_final_callback') else 'no'}\t"
        f"Report ready: {'yes' if status.get('report_ready') else 'no'}\t"
        f"Run lease: {status.get('run_lease_state', 'closed')}"
    )
    timing = status.get("timing") or {}
    if timing:
        print(
            "Timing: "
            f"wall={_format_seconds_compact(timing.get('wall_duration_s'))}\t"
            f"execution={_format_seconds_compact(timing.get('execution_duration_s'))}\t"
            f"waiting={_format_seconds_compact(timing.get('waiting_duration_s'))}\t"
            f"last_run={_format_seconds_compact(timing.get('last_run_duration_s'))}\t"
            f"evidence={timing.get('evidence_quality', 'unknown')}"
        )
    health = status.get("health") or {}
    if health:
        age = health.get("last_progress_age_s")
        age_text = f"{age}s" if isinstance(age, int) else "-"
        print(
            f"Health: {health.get('color', 'gray')} "
            f"({health.get('state', 'inactive')})\t"
            f"Last progress: {age_text}\t"
            f"Source: {health.get('source') or '-'}"
        )
    workspace = status.get("workspace") or {}
    if workspace:
        print("Path Plan:")
        print(f"  customer_dir: {workspace.get('customer_dir', '')}")
        print(f"  customer_path: {workspace.get('customer_path', '')}")
        print(f"  project_root: {workspace.get('project_root', workspace.get('root', ''))}")
        print(f"  artifact_root: {workspace.get('artifact_root', workspace.get('artifacts_dir', ''))}")
        print(f"  report_root: {workspace.get('report_root', '')}")
        print(f"Requirements: {workspace.get('task_file', '')}")
        print(f"Report: {workspace.get('report_file', '')}")
    execution = ((status.get("debug") or {}).get("execution") or status.get("execution") or {})
    if execution:
        run_id = execution.get("executor_run_id") or execution.get("worker_run_id")
        print(f"Run: {run_id or '-'}\tRunner: {execution.get('runner_status', 'pending')}")
        print(f"Live output: agentbc task logs {status['id']} --follow")
    print()
    print("Steps:")
    for index, step in enumerate(status["steps"], 1):
        step_status = step.get("status", "pending")
        description = step.get("description", "")
        print(f"  {index}. {_status_indicator(step_status)} {description}")


def _print_resolution_payload(payload: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    message = str(payload.get("message") or "").strip()
    if message:
        print(message)
    current_task = payload.get("current_task")
    if isinstance(current_task, dict):
        if message:
            print()
        _print_task_status(current_task, as_json=False)
        return
    candidates = payload.get("active_candidates") or []
    if candidates:
        print()
        print("Candidates:")
        for candidate in candidates:
            print(f"  {_format_task_candidate(candidate)}")


def _format_task_candidate(summary: dict, *, color: bool = False, timer_now: str | None = None) -> str:
    health = summary.get("health") if isinstance(summary.get("health"), dict) else {}
    health_color = str(summary.get("health_color") or health.get("color") or "gray")
    status = str(summary.get("status") or "")
    if status == "completed":
        health_color = "default"
    elif status in {"needs_recovery", "failed"}:
        health_color = "red"
    task_id = str(summary.get("task_id") or "")
    visible_task_id = task_id or str(summary.get("task_code") or "")
    dispatcher = str(summary.get("dispatcher") or summary.get("created_by") or "user")
    executor = str(summary.get("assignee") or "")
    return (
        f"{_colorize_text(visible_task_id, health_color, color=color)}\t"
        f"{summary.get('iteration', '')}\t"
        f"{dispatcher} -> {executor}\t"
        f"{_task_list_timer(summary, timer_now)}\t"
        f"{summary.get('title', '')}"
    )


def _task_list_timer(summary: dict, timer_now: str | None = None) -> str:
    """Display-only timer for the task list monitor; it is not task health.

    Uses the shared timing view's lifecycle (wall) duration so the task list
    renders the same source value as status/report/notification.
    """
    status = str(summary.get("status") or "")
    terminal_label = terminal_status_label(status)
    if terminal_label:
        return terminal_label
    if status == "input_required":
        return "input"
    if status == "cancelled":
        return "cancelled"
    timing = summary.get("timing")
    if isinstance(timing, dict):
        wall = timing.get("wall_duration_s")
        if wall is not None:
            return _format_seconds_compact(wall)
        return "unknown"
    start = str(summary.get("created_at") or "")
    if not start:
        return "unknown"
    end = str(timer_now or _utc_now_cli())
    return _format_elapsed_compact(start, end)


def _format_elapsed_compact(start: str, end: str) -> str:
    parsed_start = _parse_cli_timestamp(start)
    parsed_end = _parse_cli_timestamp(end)
    if parsed_start is None or parsed_end is None:
        return "unknown"
    seconds = max(int(round((parsed_end - parsed_start).total_seconds())), 0)
    return _format_seconds_compact(seconds)


def _format_seconds_compact(value: Any) -> str:
    try:
        seconds = max(int(round(float(value))), 0)
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _colorize_text(value: str, color_name: str, *, color: bool = False) -> str:
    clean = value or ""
    if not color:
        return clean
    colors = {
        "green": "\033[32m",
        "yellow": "\033[33m",
        "orange": "\033[38;5;208m",
        "red": "\033[31m",
        "gray": "\033[90m",
    }
    prefix = colors.get(color_name or "gray", "")
    suffix = "\033[0m" if prefix else ""
    return f"{prefix}{clean}{suffix}"


def _clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[3J\033[H", end="", flush=True)


def _status_indicator(status: str) -> str:
    return {
        "done": "✅",
        "in_progress": "🔄",
        "pending": "⏳",
        "failed": "❌",
    }.get(status, "⏳")


def _task_workspace(task, board_root: Path, config: dict | None = None) -> dict:
    workspace = dict(task.workspace or {})
    if not workspace.get("project_root"):
        workspace["project_root"] = workspace.get("root") or str(resolve_workspace_root(config))
    workspace.setdefault("root", workspace["project_root"])
    workspace.setdefault("artifact_root", workspace.get("artifacts_dir", ""))
    workspace.setdefault("report_root", str(Path(str(workspace.get("report_file", ""))).parent) if workspace.get("report_file") else "")
    code, iteration = split_task_ref(task.id)
    workspace.setdefault(
        "internal_task_dir",
        str(Path(board_root).expanduser().resolve() / code / str(iteration or "001")),
    )
    return workspace


def _task_service(board_root: str | Path, config_path: str | Path | None = None) -> TaskService:
    return TaskService(board_root, config=load_config(config_path))


def _optional_path_arg(value: Any) -> str | Path | None:
    return value if isinstance(value, (str, Path)) else None


def _print_atomic_dispatch(result: dict[str, Any]) -> None:
    task_id = str(result.get("task_id") or "")
    workspace = result.get("workspace") or {}
    print(f"created: {task_id}")
    print(f"assignee: {result.get('assignee', '')}")
    if workspace:
        print(f"task_code: {workspace.get('task_code', '')}")
        print(f"iteration: {workspace.get('iteration', '')}")
        print(f"project_root: {workspace.get('project_root', '')}")
        print(f"requirements: {workspace.get('task_file', '')}")
        print(f"artifact_root: {workspace.get('artifact_root', workspace.get('artifacts_dir', ''))}")
        print(f"report: {workspace.get('report_file', '')}")
    print(f"dispatched: {task_id}")
    print(f"worker_run_id: {result.get('run_id', '')}")
    print(f"status: {result.get('dispatch_status', '')}")
    print(f"monitor: {result.get('monitor_status', 'not_requested')}")


def _write_terminal_report(task_id: str, board_root: Path) -> None:
    from .reports import write_report_files

    write_report_files(task_id, board_root)


def _notify_terminal(
    service: TaskService,
    task_id: str,
    event_type: str,
    level: str,
    message: str,
) -> None:
    from .notifications import notify_terminal

    notify_terminal(service, task_id, event_type, level, message)


def _notify_input_required(
    service: TaskService,
    task_id: str,
    *,
    config_path: str | Path | None = None,
    interval_s: float = 2.0,
) -> dict[str, Any]:
    from .notifications import notify_input_required
    from .runner import RunnerClient

    def respond(input_id: str, response_type: str, message: str) -> dict[str, Any]:
        return RunnerClient().respond_task(
            task_id,
            input_id,
            response_type,
            message,
            service.board_root,
            config_path,
            interval_s,
        )

    return notify_input_required(service, task_id, responder=respond)


def _request_task_list_refresh(board_root: str | Path) -> None:
    from .task_health import request_dashboard_refresh

    try:
        request_dashboard_refresh(board_root)
    except OSError:
        pass


def _build_notification_payload(
    service: TaskService,
    task_id: str,
    event_type: str,
    level: str,
    message: str,
) -> dict[str, str]:
    from .notifications import build_notification_payload

    return build_notification_payload(service, task_id, event_type, level, message)


def _should_show_dialog_notification(service: TaskService, task_id: str, level: str) -> bool:
    from .notifications import should_show_dialog_notification

    return should_show_dialog_notification(service, task_id, level)


def _compact_notification_text(value: str, limit: int) -> str:
    from .notifications import compact_notification_text

    return compact_notification_text(value, limit)


def _format_elapsed(start: str, end: str) -> str:
    from .notifications import format_elapsed

    return format_elapsed(start, end)


def _parse_cli_timestamp(value: str) -> datetime | None:
    from .notifications import parse_timestamp

    return parse_timestamp(value)


def _utc_now_cli() -> str:
    from .notifications import utc_now

    return utc_now()


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        parser = build_parser()
        parser.print_help()
        return 0
    expanded_argv = _expand_shorthand(raw_argv)
    if expanded_argv[0] == "_shorthand":
        shorthand_parser = argparse.ArgumentParser(prog="agentbc")
        add_task_root(shorthand_parser)
        shorthand_parser.add_argument("description", nargs="+")
        args = shorthand_parser.parse_args(expanded_argv[1:])
        args.description = " ".join(args.description)
        return command_shorthand(args)

    parser = build_parser()
    args = parser.parse_args(expanded_argv)

    if args.command == "setup":
        from .setup import run_clean, run_setup, run_show, run_update

        interactive = not args.non_interactive
        if args.show:
            result = run_show()
        elif args.update:
            result = run_update(interactive=interactive)
        elif args.clean:
            result = run_clean(interactive=interactive)
        else:
            result = run_setup(
                interactive=interactive,
                permission_mode=getattr(args, "permission_mode", None),
            )
            from .runner import start_runner_background

            runner_result = start_runner_background(config_path=result.get("config_path"))
            result["runner"] = runner_result
            if not runner_result.get("ok"):
                result["ok"] = False
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if args.command == "doctor":
        return command_doctor(args)

    if args.command == "uninstall":
        from .setup import run_uninstall

        try:
            result = run_uninstall(
                interactive=not args.non_interactive,
                remove_records=args.remove_records,
                remove_artifacts=args.remove_artifacts,
            )
        except (RuntimeError, ValueError) as exc:
            print(f"uninstall_error: {exc}")
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if args.command == "init":
        init_board(args.root)
        print(f"initialized: {Path(args.root).expanduser().resolve()}")
        return 0

    if args.command == "record":
        if args.record_command == "clean":
            from .record_management import clean_terminal_records

            result = clean_terminal_records(args.root, dry_run=args.dry_run)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        raise AssertionError(args.record_command)

    if args.command == "task":
        if args.task_command == "create":
            return command_task_create(args)
        if args.task_command == "list":
            return command_task_list(args)
        if args.task_command == "status":
            return command_task_status(args)
        if args.task_command == "report":
            return command_task_report(args)
        if args.task_command == "logs":
            return command_task_logs(args)
        if args.task_command == "progress":
            return command_task_progress(args)
        if args.task_command == "dispatch":
            return command_task_dispatch(args)
        if args.task_command == "respond":
            return command_task_respond(args)
        if args.task_command == "preflight":
            return command_task_preflight(args)
        if args.task_command == "recover":
            return command_task_recover(args)
        if args.task_command == "callback":
            return command_task_callback(args)
        if args.task_command in {"pause", "resume", "cancel", "close", "correct", "retry", "reassign", "handoff"}:
            return command_task_intervention(args)
        raise AssertionError(args.task_command)

    if args.command == "worker":
        if args.worker_command == "run":
            return command_worker_run(args)
        if args.worker_command == "reap":
            return command_worker_reap(args)
        raise AssertionError(args.worker_command)

    if args.command == "runner":
        return command_runner(args)
    raise AssertionError(args.command)


def _expand_shorthand(argv: list[str]) -> list[str]:
    known_commands = {
        "setup",
        "doctor",
        "uninstall",
        "init",
        "record",
        "task",
        "worker",
        "runner",
    }
    first = argv[0]
    if first.startswith("-") or first in known_commands:
        return argv
    alias = _SHORTHAND_ALIASES.get(first.lower())
    if alias is not None:
        return [*alias, *argv[1:]]
    if is_task_like(first):
        return ["task", "status", first.upper(), *argv[1:]]
    return ["_shorthand", *argv]


def _should_reject_shorthand(description: str) -> bool:
    token = description.strip()
    if not token or any(character.isspace() for character in token):
        return False
    if is_task_like(token):
        return False
    return token.replace("-", "").replace("_", "").isalnum()


def _suggest_shorthand_command(description: str) -> str:
    match = difflib.get_close_matches(description, _SHORTHAND_SUGGESTIONS, n=1, cutoff=0.6)
    return match[0] if match else ""


if __name__ == "__main__":
    raise SystemExit(main())
