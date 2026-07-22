from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import ExecutorPort, PollResult
from agent_bridge_connect.run_lease import (
    RunLease,
    RunLeaseState,
    close_lease,
    create_lease,
    heartbeat,
    save_lease,
)


class CLIExecutorBase(ExecutorPort):
    """Base for blocking CLI adapters that cache results for poll()."""

    def __init__(self) -> None:
        self._runs: dict[str, PollResult] = {}
        self._run_leases: dict[str, RunLease] = {}
        self._lease_roots: dict[str, Path] = {}

    def poll(self, run_id: str) -> PollResult:
        return self._runs.get(
            run_id,
            PollResult(status="failed", result={"error": f"unknown run_id: {run_id}"}),
        )

    def _start_run_lease(
        self,
        task_packet: dict[str, Any],
        run_id: str,
        executor_id: str,
        pid: int | None = None,
    ) -> RunLease:
        workspace = task_packet.get("workspace") or {}
        workspace_root = Path(
            str(workspace.get("root") or ".")
        ).expanduser().resolve()
        task_board = task_packet.get("task_board") or {}
        lease_root = Path(
            str(task_board.get("root") or workspace_root)
        ).expanduser().resolve()
        lease = create_lease(
            task_id=str(task_packet.get("task_id") or "unknown"),
            executor_id=executor_id,
            pid=os.getpid() if pid is None else pid,
            work_dir=str(workspace_root),
        )
        lease.run_id = run_id
        self._run_leases[run_id] = lease
        self._lease_roots[run_id] = lease_root
        save_lease(lease, lease_root)
        return lease

    def _heartbeat_run(self, run_id: str) -> None:
        lease = self._run_leases.get(run_id)
        if lease is None:
            return
        heartbeat(lease)

    def _mark_run_stale(self, run_id: str) -> None:
        lease = self._run_leases.get(run_id)
        if lease is None:
            return
        lease.state = RunLeaseState.STALE
        save_lease(lease, self._lease_roots[run_id])

    def _close_run_lease(self, run_id: str) -> None:
        lease = self._run_leases.get(run_id)
        if lease is None:
            return
        close_lease(lease, self._lease_roots[run_id])
