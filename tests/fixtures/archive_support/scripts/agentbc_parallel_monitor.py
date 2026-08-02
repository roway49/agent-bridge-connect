#!/usr/bin/env python3
"""Temporary AgentBC parallel-run monitor.

This script is intentionally read-only. It samples process pressure, Runner
health, task state, network reachability, task-list UI presence, and manual
events into JSONL for later analysis.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_PATTERNS = ("agentbc", "hermes", "codex", "claude")
TERMINAL_STATES = {"completed", "failed", "cancelled", "input_required", "needs_recovery", "rejected"}
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(argv: list[str], timeout: float = 3.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001 - monitor must not crash on transient tool failures.
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "elapsed_s": round(time.monotonic() - started, 3),
        }


def parse_ps_line(line: str) -> Optional[dict[str, Any]]:
    parts = line.strip().split(None, 6)
    if len(parts) < 7:
        return None
    pid, ppid, pcpu, pmem, rss, state, etime_cmd = parts
    etime_parts = etime_cmd.split(None, 1)
    if len(etime_parts) != 2:
        return None
    etime, command = etime_parts
    try:
        return {
            "pid": int(pid),
            "ppid": int(ppid),
            "pcpu": float(pcpu),
            "pmem": float(pmem),
            "rss_kb": int(rss),
            "state": state,
            "etime": etime,
            "command": command,
        }
    except ValueError:
        return None


def sample_processes(patterns: tuple[str, ...]) -> dict[str, Any]:
    runner_sample = sample_runner_processes(patterns)
    if runner_sample["ok"]:
        return runner_sample
    ps = run_command(["ps", "-axo", "pid=,ppid=,pcpu=,pmem=,rss=,state=,etime=,command="], timeout=5.0)
    rows: list[dict[str, Any]] = []
    if ps["ok"]:
        lowered = tuple(p.lower() for p in patterns)
        for line in ps["stdout"].splitlines():
            row = parse_ps_line(line)
            if not row:
                continue
            command = row["command"].lower()
            if any(pattern in command for pattern in lowered) or "agent_bridge_connect" in command:
                row["group"] = classify_process(row["command"])
                rows.append(row)
    rows.sort(key=lambda item: item["pcpu"], reverse=True)
    cpu_sum = sum(row["pcpu"] for row in rows)
    rss_sum = sum(row["rss_kb"] for row in rows)
    groups = summarize_process_groups(rows)
    return {
        "source": "local_ps",
        "count": len(rows),
        "cpu_sum": round(cpu_sum, 2),
        "rss_kb_sum": rss_sum,
        "rss_mb_sum": round(rss_sum / 1024, 1),
        "groups": groups,
        "top": rows[:10],
        "ps_ok": ps["ok"],
        "ps_error": ps["stderr"],
        "runner_sample_error": runner_sample.get("stderr") or runner_sample.get("error"),
    }


def sample_runner_processes(patterns: tuple[str, ...]) -> dict[str, Any]:
    argv = ["agentbc", "runner", "process-sample"]
    for pattern in patterns:
        argv.extend(["--pattern", pattern])
    result = run_command(argv, timeout=7.0)
    if not result["ok"] or not result["stdout"]:
        return {
            "ok": False,
            "source": "runner_ps",
            "stderr": result["stderr"],
            "stdout": result["stdout"],
            "elapsed_s": result["elapsed_s"],
        }
    try:
        parsed = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "source": "runner_ps",
            "error": f"invalid runner process sample: {exc}",
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "elapsed_s": result["elapsed_s"],
        }
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "source": "runner_ps",
            "error": "runner process sample is not an object",
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "elapsed_s": result["elapsed_s"],
        }
    parsed.setdefault("source", "runner_ps")
    parsed["runner_elapsed_s"] = result["elapsed_s"]
    return parsed


def classify_process(command: str) -> str:
    lowered = command.lower()
    if "agent_bridge_connect.cli task list" in lowered:
        return "agentbc_task_list"
    if "agent_bridge_connect.cli worker run" in lowered:
        return "agentbc_worker"
    if "agentbc runner serve" in lowered or "agent_bridge_connect.cli runner serve" in lowered:
        return "agentbc_runner"
    if "codex.app" in lowered or "com.openai.codex" in lowered:
        return "codex_gui"
    if "codex exec" in lowered or lowered.endswith("/codex") or "resources/codex" in lowered:
        return "codex_cli"
    if "hermes_cli.main dashboard" in lowered or "hermes.app" in lowered:
        return "hermes_gui"
    if "/hermes" in lowered or " hermes " in lowered:
        return "hermes_cli"
    if "/claude" in lowered or " claude " in lowered:
        return "claude_cli"
    if "agent_bridge_connect" in lowered or "agentbc" in lowered:
        return "agentbc_other"
    return "matched_other"


def summarize_process_groups(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = str(row.get("group") or "matched_other")
        current = groups.setdefault(group, {"count": 0, "cpu_sum": 0.0, "rss_kb_sum": 0})
        current["count"] += 1
        current["cpu_sum"] += float(row.get("pcpu") or 0.0)
        current["rss_kb_sum"] += int(row.get("rss_kb") or 0)
    for data in groups.values():
        data["cpu_sum"] = round(float(data["cpu_sum"]), 2)
        data["rss_mb_sum"] = round(int(data["rss_kb_sum"]) / 1024, 1)
    return dict(sorted(groups.items()))


def sample_runner() -> dict[str, Any]:
    result = run_command(["agentbc", "runner", "status"], timeout=5.0)
    parsed: Any = None
    if result["stdout"]:
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            parsed = None
    return {
        "ok": result["ok"],
        "parsed": parsed,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "elapsed_s": result["elapsed_s"],
    }


def sample_task(task_id: str, board_root: str = "") -> dict[str, Any]:
    argv = ["agentbc", "task", "status", task_id, "--json"]
    if board_root:
        argv.extend(["--root", board_root])
    result = run_command(argv, timeout=5.0)
    parsed: Any = None
    if result["stdout"]:
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            parsed = None
    current = parsed.get("current_task") if isinstance(parsed, dict) else None
    status = current.get("status") if isinstance(current, dict) else None
    execution = current.get("execution") if isinstance(current, dict) else None
    health = current.get("health") if isinstance(current, dict) else None
    return {
        "task_id": task_id,
        "ok": result["ok"],
        "status": status,
        "terminal": status in TERMINAL_STATES,
        "health": health,
        "execution": execution,
        "stderr": result["stderr"],
        "elapsed_s": result["elapsed_s"],
    }


def sample_task_list(board_root: str = "") -> dict[str, Any]:
    argv = ["agentbc", "task", "list", "--current"]
    if board_root:
        argv.extend(["--root", board_root])
    result = run_command(argv, timeout=5.0)
    return {
        "ok": result["ok"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "elapsed_s": result["elapsed_s"],
    }


def sample_terminal_windows() -> dict[str, Any]:
    script = (
        'tell application "Terminal"\n'
        '  set matches to {}\n'
        '  repeat with w in windows\n'
        '    try\n'
        '      if name of w contains "AgentBC Task List" then set end of matches to (id of w as text)\n'
        '    end try\n'
        '  end repeat\n'
        '  return matches as text\n'
        'end tell\n'
    )
    result = run_command(["osascript", "-e", script], timeout=5.0)
    ids: list[str] = []
    if result["ok"] and result["stdout"]:
        ids = [part.strip() for part in result["stdout"].replace(",", " ").split() if part.strip()]
    return {
        "ok": result["ok"],
        "count": len(ids),
        "ids": ids,
        "stderr": result["stderr"],
        "elapsed_s": result["elapsed_s"],
    }


def sample_network(probe: str) -> dict[str, Any]:
    result = run_command(["ping", "-c", "1", "-W", "1000", probe], timeout=3.0)
    return {
        "probe": probe,
        "ok": result["ok"],
        "stdout_tail": result["stdout"].splitlines()[-1:] if result["stdout"] else [],
        "stderr": result["stderr"],
        "elapsed_s": result["elapsed_s"],
    }


def sample_disk(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": str(path),
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
        }
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}


def read_manual_events(path: Path) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line})
    return events


def discover_suite_tasks(board_root: Path, suite_id: str) -> list[str]:
    index_file = board_root.expanduser() / "task_index.jsonl"
    try:
        lines = index_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if suite_id not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_id = str(entry.get("task_id") or "").strip()
        if task_id and task_id not in seen:
            found.append(task_id)
            seen.add(task_id)
    return sorted(found)


def _need_sample(now: float, last_at: float, interval: float) -> bool:
    return last_at <= 0 or now - last_at >= max(interval, 0.5)


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_summary(path: Path, records: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if not records:
        return
    process_records = [record for record in records if record.get("processes")]
    peak_cpu = max((record["processes"]["cpu_sum"] for record in process_records), default=0)
    peak_rss = max((record["processes"]["rss_mb_sum"] for record in process_records), default=0)
    peak_count = max((record["processes"]["count"] for record in process_records), default=0)
    peak_groups: dict[str, dict[str, Any]] = {}
    for record in process_records:
        for group, data in (record["processes"].get("groups") or {}).items():
            current = peak_groups.setdefault(group, {"peak_rss_mb": 0.0, "peak_cpu": 0.0, "peak_count": 0})
            current["peak_rss_mb"] = max(float(current["peak_rss_mb"]), float(data.get("rss_mb_sum") or 0.0))
            current["peak_cpu"] = max(float(current["peak_cpu"]), float(data.get("cpu_sum") or 0.0))
            current["peak_count"] = max(int(current["peak_count"]), int(data.get("count") or 0))
    runner_samples = [record for record in records if record.get("runner")]
    runner_failures = sum(1 for record in runner_samples if not record["runner"]["ok"])
    task_samples: dict[str, dict[str, Any]] = {}
    for record in records:
        for task in record.get("tasks") or []:
            task_samples[str(task.get("task_id") or "")] = task
    network_samples = [record["network"] for record in records if record.get("network")]
    network_failures = sum(1 for item in network_samples if not item.get("ok"))
    terminal_samples = [record["terminal_windows"] for record in records if record.get("terminal_windows")]
    peak_task_list_windows = max((item.get("count", 0) for item in terminal_samples), default=0)
    slow_task_list = [
        record["task_list_current"]["elapsed_s"]
        for record in records
        if record.get("task_list_current") and record["task_list_current"].get("elapsed_s", 0) > 2
    ]
    manual_events = read_manual_events(args.events_file) if args.events_file else []
    lines = [
        "# AgentBC Parallel Monitor Summary",
        "",
        f"- Suite: `{args.suite_id}`",
        f"- Samples: `{len(records)}`",
        f"- Started: `{records[0]['ts']}`",
        f"- Ended: `{records[-1]['ts']}`",
        f"- Peak matched processes: `{peak_count}`",
        f"- Peak matched CPU sum: `{peak_cpu}`",
        f"- Peak matched RSS: `{peak_rss} MB`",
        f"- Runner status samples: `{len(runner_samples)}`",
        f"- Runner status failures: `{runner_failures}`",
        f"- Network samples: `{len(network_samples)}`",
        f"- Network failures: `{network_failures}`",
        f"- Peak AgentBC Task List windows: `{peak_task_list_windows}`",
        f"- Slow task-list samples >2s: `{len(slow_task_list)}`",
        f"- Manual events: `{len(manual_events)}`",
        "",
        "## Peak Process Groups",
        "",
        "| Group | Peak Count | Peak CPU | Peak RSS MB |",
        "|---|---:|---:|---:|",
    ]
    if peak_groups:
        for group, data in sorted(peak_groups.items(), key=lambda item: item[1]["peak_rss_mb"], reverse=True):
            lines.append(
                "| `{}` | `{}` | `{}` | `{}` |".format(
                    group,
                    data["peak_count"],
                    data["peak_cpu"],
                    data["peak_rss_mb"],
                )
            )
    else:
        lines.append("| _none_ | `0` | `0` | `0` |")
    lines.extend(
        [
            "",
            "## Final Task Snapshot",
            "",
        ]
    )
    if task_samples:
        lines.append("| Task | Status | Terminal | Health | Runner |")
        lines.append("|---|---|---|---|---|")
        for task_id in sorted(task_samples):
            task = task_samples[task_id]
            execution = task.get("execution") or {}
            health = task.get("health") or {}
            lines.append(
                "| `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                    task.get("task_id", ""),
                    task.get("status", ""),
                    task.get("terminal", ""),
                    health.get("color") or health.get("state") or "",
                    execution.get("runner_status", ""),
                )
            )
    else:
        lines.append("_No task IDs were sampled._")
    if manual_events:
        lines.extend(["", "## Manual Events", ""])
        lines.append("| At | Event | Note |")
        lines.append("|---|---|---|")
        for event in manual_events[-20:]:
            lines.append(
                "| `{}` | `{}` | {} |".format(
                    event.get("ts", ""),
                    event.get("event", event.get("type", "")),
                    str(event.get("note", event.get("raw", ""))).replace("|", "\\|"),
                )
            )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def handle_signal(signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor AgentBC parallel task pressure.")
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--discover-suite", action="store_true", help="Discover suite task IDs from task_index.jsonl.")
    parser.add_argument("--board-root", default="", help="Task board root used by agentbc task commands.")
    parser.add_argument("--events-file", type=Path, help="Manual JSONL events file to include in samples and summary.")
    parser.add_argument("--interval", type=float, default=5.0, help="Main loop/process sampling interval.")
    parser.add_argument("--process-interval", type=float, default=5.0)
    parser.add_argument("--runner-interval", type=float, default=5.0)
    parser.add_argument("--task-status-interval", type=float, default=20.0)
    parser.add_argument("--task-list-interval", type=float, default=20.0)
    parser.add_argument("--network-interval", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=0.0, help="Seconds to run; 0 means until Ctrl-C.")
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--task-list", action="store_true", help="Also capture `agentbc task list --current` text.")
    parser.add_argument("--terminal-check", action="store_true", help="Sample AgentBC Task List Terminal windows with osascript.")
    parser.add_argument("--network-probe", default="", help="Host/IP to ping, e.g. 1.1.1.1.")
    parser.add_argument(
        "--disk-path",
        default=str(Path.home() / "Documents" / "AgentBC" / "workspace"),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    patterns = tuple(args.pattern or DEFAULT_PATTERNS)
    records: list[dict[str, Any]] = []
    task_ids = list(dict.fromkeys(args.task_id))
    board_root = str(Path(args.board_root).expanduser()) if args.board_root else ""
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    last_process = last_runner = last_tasks = last_task_list = last_network = last_terminal = 0.0
    last_seen_processes: dict[str, Any] | None = None
    last_seen_runner: dict[str, Any] | None = None
    last_seen_tasks: list[dict[str, Any]] = []

    while not STOP:
        now = time.monotonic()
        if args.discover_suite and board_root and _need_sample(now, last_tasks, args.task_status_interval):
            for discovered in discover_suite_tasks(Path(board_root), args.suite_id):
                if discovered not in task_ids:
                    task_ids.append(discovered)

        record: dict[str, Any] = {
            "ts": utc_now(),
            "suite_id": args.suite_id,
            "load_avg": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "task_ids": task_ids,
            "processes": None,
            "runner": None,
            "tasks": None,
            "task_list_current": None,
            "terminal_windows": None,
            "network": None,
            "manual_events_count": len(read_manual_events(args.events_file)) if args.events_file else 0,
            "disk": sample_disk(Path(args.disk_path).expanduser()),
        }

        if _need_sample(now, last_process, args.process_interval):
            last_seen_processes = sample_processes(patterns)
            last_process = now
        if _need_sample(now, last_runner, args.runner_interval):
            last_seen_runner = sample_runner()
            last_runner = now
        if task_ids and _need_sample(now, last_tasks, args.task_status_interval):
            last_seen_tasks = [sample_task(task_id, board_root) for task_id in task_ids]
            last_tasks = now
        if args.task_list and _need_sample(now, last_task_list, args.task_list_interval):
            record["task_list_current"] = sample_task_list(board_root)
            last_task_list = now
        if args.terminal_check and _need_sample(now, last_terminal, args.task_list_interval):
            record["terminal_windows"] = sample_terminal_windows()
            last_terminal = now
        if args.network_probe and _need_sample(now, last_network, args.network_interval):
            record["network"] = sample_network(args.network_probe)
            last_network = now

        record["processes"] = last_seen_processes
        record["runner"] = last_seen_runner
        record["tasks"] = last_seen_tasks
        write_jsonl(args.output, record)
        records.append(record)
        processes = record["processes"] or {}
        runner = record["runner"] or {}
        print(
            "{} suite={} tasks={} procs={} cpu={} rss_mb={} runner_ok={}".format(
                record["ts"],
                args.suite_id,
                len(task_ids),
                processes.get("count"),
                processes.get("cpu_sum"),
                processes.get("rss_mb_sum"),
                runner.get("ok"),
            ),
            flush=True,
        )
        if task_ids and last_seen_tasks and all(task.get("terminal") for task in last_seen_tasks):
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(max(args.interval, 0.5))

    if args.summary:
        write_summary(args.summary, records, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
