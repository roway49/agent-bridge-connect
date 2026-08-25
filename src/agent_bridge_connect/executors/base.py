from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_bridge_connect.adapters import ExecutorPort, PollResult
from agent_bridge_connect.control import ApprovalControlPlane, ControlPlaneError
from agent_bridge_connect.run_lease import (
    RunLease,
    RunLeaseState,
    close_lease,
    create_lease,
    heartbeat,
    load_lease,
    save_lease,
    suspend_lease,
)
from agent_bridge_connect.session import control_root_for_task


class CLIExecutorBase(ExecutorPort):
    """Base for blocking CLI adapters that cache results for poll()."""

    def __init__(self) -> None:
        self._runs: dict[str, PollResult] = {}
        self._run_leases: dict[str, RunLease] = {}
        self._lease_roots: dict[str, Path] = {}
        self._control_planes: dict[str, ApprovalControlPlane] = {}
        self._task_packets: dict[str, dict[str, Any]] = {}

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

    def _control_plane_for_run(
        self,
        task_packet: dict[str, Any],
        run_id: str,
        *,
        expected_session_id: str | None = None,
        create: bool = True,
    ) -> ApprovalControlPlane:
        """Return the exact task-scoped control plane for one adapter run."""
        existing = self._control_planes.get(run_id)
        if existing is not None:
            return existing
        task_id = str(task_packet.get("task_id") or "").strip()
        task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
        board_root = str(task_board.get("root") or "").strip()
        if not task_id or not board_root:
            raise ControlPlaneError(
                "control_identity_missing",
                "Task control requires an exact task ID and task board root.",
            )
        extensions = task_packet.get("extensions") if isinstance(task_packet.get("extensions"), dict) else {}
        session = extensions.get("agentbc.session") if isinstance(extensions.get("agentbc.session"), dict) else {}
        expected = expected_session_id
        if expected is None and session.get("run_ids"):
            expected = str(session.get("session_id") or "").strip() or None
        root = control_root_for_task(task_id, board_root=board_root)
        plane = ApprovalControlPlane(
            root,
            task_id=task_id,
            executor_run_id=run_id,
            session_id=expected or "",
            executor=str(task_packet.get("assignee") or "codex"),
            expected_session_id=expected,
            create=create,
        )
        self._control_planes[run_id] = plane
        return plane

    def _suspend_run(self, run_id: str) -> RunLease | None:
        lease = self._run_leases.get(run_id)
        lease_root = self._lease_roots.get(run_id)
        if lease is None or lease_root is None:
            return None
        suspended = suspend_lease(
            lease.task_id,
            lease_root,
            executor_run_id=run_id,
            executor_id=lease.executor_id,
            work_dir=lease.work_dir,
        )
        self._run_leases[run_id] = suspended
        return suspended

    def _resume_run(self, run_id: str) -> RunLease | None:
        lease = self._run_leases.get(run_id)
        lease_root = self._lease_roots.get(run_id)
        if lease is None or lease_root is None:
            return None
        persisted = load_lease(lease.task_id, lease_root)
        if persisted is None or persisted.run_id != run_id:
            raise ControlPlaneError(
                "run_lease_mismatch",
                "Cannot resume a run lease that does not match the executor run.",
            )
        persisted.state = RunLeaseState.ACTIVE
        persisted.pid = os.getpid()
        persisted.pgid = os.getpgid(os.getpid())
        persisted.cleanup_strategy = "none"
        save_lease(persisted, lease_root)
        self._run_leases[run_id] = persisted
        return persisted

    def _record_transport_failed(
        self,
        run_id: str,
        reason: str,
        *,
        request_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        plane = self._control_planes.get(run_id)
        if plane is None:
            return None
        return plane.record_transport_failed(reason, request_id=request_id, evidence=evidence)

    def respond_approval(
        self,
        task_id: str,
        executor_run_id: str,
        session_id: str,
        request_id: str,
        decision: str,
    ) -> dict[str, Any]:
        plane = self._control_planes.get(executor_run_id)
        if plane is None:
            raise ControlPlaneError(
                "approval_control_unavailable",
                "No active adapter control plane exists for this run.",
            )
        return plane.respond_approval(task_id, executor_run_id, session_id, request_id, decision)

    def control_status(self, run_id: str) -> dict[str, Any]:
        plane = self._control_planes.get(run_id)
        if plane is None:
            raise ControlPlaneError("approval_control_unavailable", "No control plane exists for this run.")
        return plane.status()

    def control_events(self, run_id: str) -> list[dict[str, Any]]:
        plane = self._control_planes.get(run_id)
        if plane is None:
            raise ControlPlaneError("approval_control_unavailable", "No control plane exists for this run.")
        return plane.events()

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
