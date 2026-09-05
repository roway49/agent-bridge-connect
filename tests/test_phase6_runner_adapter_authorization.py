from __future__ import annotations

import copy
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_bridge_connect.effective_permissions import resolve_effective_permission
from agent_bridge_connect.executors.claude import ClaudeExecutor
from agent_bridge_connect.executors.codex import CodexExecutor
from agent_bridge_connect.executors.hermes import HermesExecutor
from agent_bridge_connect.permission_grants import (
    PERMISSION_GRANT_EXTENSION_KEY,
    build_permission_grant,
)
from agent_bridge_connect.permission_modes import permission_flags
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.runner import (
    CLAUDE_SDK_CONTROL_AUTHORIZATION,
    RunnerClient,
    RunnerError,
    RunnerState,
)
from agent_bridge_connect.service import TaskService


class Phase6RunnerAdapterAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.root / "record"
        self.binaries = {
            name: self.root / name for name in ("codex", "claude", "hermes")
        }
        full_flags = {
            "codex": "--dangerously-bypass-approvals-and-sandbox",
            "claude": "--dangerously-skip-permissions",
            "hermes": "--yolo",
        }
        for name, path in self.binaries.items():
            path.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                f"  *--help*) printf '%s\\n' '{full_flags[name]}'; exit 0;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.state = RunnerState(
            self.root / "runner",
            [self.root],
            self.binaries,
        )

    def _grant_packet(self, executor: str) -> tuple[TaskService, dict, str, str]:
        config = {
            "workspace_root": str(self.root / "workspace"),
            "executors": {
                "claude": {"max_budget_usd": 10.0},
                "hermes": {"max_turns": 90},
            },
            "sessions": {"retain_executor_sessions": True},
        }
        service = TaskService(self.board, config=config)
        task = service.create_task(
            f"Phase 6 {executor}",
            executor,
            [{"id": 1, "description": "continue with one-shot full"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        raw = service.store.read_task(task.id)
        source_run_id = f"{executor}-{task.id}-source"
        session_ids = {
            "codex": "019feed0-0000-7000-8000-000000000006",
            "claude": "019feed0-0000-7000-8000-000000000007",
            "hermes": "20260812_010203_phase6",
        }
        session_id = session_ids[executor]
        session = dict(raw["extensions"]["agentbc.session"])
        session.update(
            {
                "session_id": session_id,
                "session_state": "active",
                "run_ids": [source_run_id],
            }
        )
        input_id = f"input-{executor}-phase6"
        input_request = {
            "input_id": input_id,
            "executor_run_id": source_run_id,
            "blocked_step_id": 1,
            "type": "permission",
            "requested_permission": "full",
            "status": "answered",
            "response": {"type": "approve", "summary": "approve"},
        }
        grant = build_permission_grant(
            executor=executor,
            task_id=task.id,
            input_id=input_id,
            session_id=session_id,
            source_run_id=source_run_id,
        )
        raw["status"] = "running"
        raw["extensions"]["agentbc.execution"]["internal_status"] = "resuming"
        raw["extensions"]["agentbc.execution"]["resuming_at"] = raw["updated_at"]
        raw["extensions"]["agentbc.session"] = session
        raw["extensions"]["agentbc.input"] = input_request
        raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY] = grant
        service.store.write_task(task.id, raw)
        packet = copy.deepcopy(raw)
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(self.board)}
        packet["runner_authorization_required"] = True
        return service, packet, source_run_id, session_id

    def _command(self, executor: str, packet: dict, *, full: bool = True) -> list[str]:
        session = packet["extensions"]["agentbc.session"]
        mode = "full" if full else "safe"
        if executor == "codex":
            return [
                str(self.binaries[executor]),
                "exec",
                "--json",
                *permission_flags(executor, mode),
                "--skip-git-repo-check",
                "resume",
                session["session_id"],
                "prompt",
            ]
        if executor == "claude":
            resources = packet["extensions"]["agentbc.resources"]
            return [
                str(self.binaries[executor]),
                "-p",
                *permission_flags(executor, mode),
                "--resume",
                session["session_id"],
                "--output-format",
                "text",
                "--max-budget-usd",
                str(float(resources["current_limit"])),
                "prompt",
            ]
        resources = packet["extensions"]["agentbc.resources"]
        return [
            str(self.binaries[executor]),
            "chat",
            *permission_flags(executor, mode),
            "--max-turns",
            str(int(resources["current_limit"])),
            "--resume",
            session["session_id"],
            "-Q",
            "-q",
            "prompt",
        ]

    def _cwd(self, executor: str, packet: dict) -> str:
        if executor == "claude":
            return packet["extensions"]["agentbc.session"]["project_path"]
        return str(self.project)

    def test_issued_grant_prepares_outer_containment_before_worker_spawn(self) -> None:
        service, packet, _source_run_id, _session_id = self._grant_packet("hermes")
        task_id = packet["task_id"]
        fake_run = {
            "ok": True,
            "run_id": "runner-worker-123456789abc",
            "pid": 42,
            "status": "running",
        }
        with mock.patch.object(
            self.state, "_spawn_process", return_value=fake_run
        ) as spawn:
            result = self.state.dispatch_worker(
                task_id,
                "hermes",
                str(self.board),
                "",
                0.2,
                False,
                resuming=True,
            )
        self.assertEqual(result["dispatch_status"], "accepted")
        _args, kwargs = spawn.call_args
        self.assertIsInstance(kwargs.get("containment"), dict)
        runtime = service.get_task(task_id).extensions["agentbc.permission_runtime"]
        self.assertEqual(
            runtime["binding"]["permission_source"],
            "one_shot_permission_grant",
        )
        self.assertEqual(runtime["state"]["status"], "activated")
        # The outer Worker is contained first.  The exact Adapter run later
        # consumes the grant through RunnerClient.authorize_command.
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "issued")

    def test_common_resolver_upgrades_only_the_approved_target_context(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                _service, packet, _source_run_id, session_id = self._grant_packet(
                    executor
                )
                target = f"{executor}-{packet['task_id']}-target"
                with self.assertRaises(ABCError) as unmanaged:
                    resolve_effective_permission(packet, executor, target)
                self.assertEqual(
                    unmanaged.exception.code,
                    "permission_grant_runner_context_required",
                )
                resolved = resolve_effective_permission(
                    packet,
                    executor,
                    target,
                    trusted_runner_managed=True,
                )
                self.assertEqual(resolved["effective_mode"], "full")
                self.assertTrue(resolved["temporary"])
                self.assertEqual(resolved["executor_run_id"], target)
                self.assertEqual(
                    packet["extensions"]["agentbc.session"]["session_id"],
                    session_id,
                )
                worker_packet = copy.deepcopy(packet)
                worker_packet.pop("id", None)
                worker_packet.pop("status", None)
                worker_packet.pop("assignee", None)
                worker_packet["extensions"]["agentbc.execution"][
                    "internal_status"
                ] = "running"
                self.assertEqual(
                    resolve_effective_permission(
                        worker_packet,
                        executor,
                        f"{target}-worker",
                        trusted_runner_managed=True,
                    )["effective_mode"],
                    "full",
                )

        _service, hermes, _source, _session = self._grant_packet("hermes")
        hermes["extensions"]["agentbc.session"]["session_id"] = ""
        with self.assertRaises(ABCError) as raised:
            resolve_effective_permission(
                hermes,
                "hermes",
                "hermes-target",
                trusted_runner_managed=True,
            )
        self.assertEqual(raised.exception.code, "permission_grant_context_invalid")

    def test_persisted_base_modes_resolve_without_runner_context(self) -> None:
        for mode in ("inherit", "safe", "full"):
            with self.subTest(mode=mode):
                resolved = resolve_effective_permission(
                    {
                        "extensions": {
                            "agentbc.permission": {
                                "requested_mode": mode,
                                "effective_mode": mode,
                                "selection_source": "explicit_task",
                            }
                        }
                    },
                    "codex",
                    f"codex-base-{mode}",
                )
                self.assertEqual(resolved["effective_mode"], mode)

    def _assert_unmanaged_adapter_rejected(self, executor_name: str) -> None:
        service, packet, _source, _session = self._grant_packet(executor_name)
        packet.pop("runner_authorization_required")
        executors = {
            "codex": CodexExecutor(command=str(self.binaries["codex"])),
            "claude": ClaudeExecutor(
                command=str(self.binaries["claude"]), transport="direct"
            ),
            "hermes": HermesExecutor(
                command=str(self.binaries["hermes"]), transport="direct"
            ),
        }
        executor = executors[executor_name]
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch.object(executor, "_build_command") as build_command,
            mock.patch(
                f"agent_bridge_connect.executors.{executor_name}.subprocess.run"
            ) as spawn,
            mock.patch(
                f"agent_bridge_connect.executors.{executor_name}.RunnerClient.authorize_command"
            ) as authorize,
        ):
            result = executor.start(packet)
        self.assertFalse(result.ok)
        self.assertIn("permission_grant_runner_context_required", result.message)
        build_command.assert_not_called()
        spawn.assert_not_called()
        authorize.assert_not_called()
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_codex_unmanaged_issued_grant_fails_before_argv_or_spawn(self) -> None:
        self._assert_unmanaged_adapter_rejected("codex")

    def test_claude_unmanaged_issued_grant_fails_before_argv_or_spawn(self) -> None:
        self._assert_unmanaged_adapter_rejected("claude")

    def test_hermes_unmanaged_issued_grant_fails_before_argv_or_spawn(self) -> None:
        self._assert_unmanaged_adapter_rejected("hermes")

    def test_runner_authorization_consumes_once_and_rejects_other_targets(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        target = f"codex-{packet['task_id']}-approved"
        with mock.patch(
            "agent_bridge_connect.runner.resolve_effective_permission",
            wraps=resolve_effective_permission,
        ) as resolver:
            result = self.state.authorize_command(
                "codex",
                self._command("codex", packet),
                self._cwd("codex", packet),
                packet,
                target,
            )
        self.assertEqual(result["effective_permission_mode"], "full")
        resolved_task = resolver.call_args.args[0]
        self.assertEqual(
            resolved_task["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["state"][
                "status"
            ],
            "consumed",
        )
        self.assertIs(
            resolver.call_args.kwargs["trusted_runner_managed"],
            True,
        )
        persisted = service.store.read_task(packet["task_id"])
        consumed = persisted["extensions"][PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(consumed["state"], {"status": "consumed", "uses": 1})
        self.assertEqual(consumed["binding"]["target_run_id"], target)

        for replay_target in (target, f"{target}-other"):
            with self.subTest(replay_target=replay_target):
                with self.assertRaises(RunnerError):
                    self.state.authorize_command(
                        "codex",
                        self._command("codex", packet),
                        self._cwd("codex", packet),
                        packet,
                        replay_target,
                    )
        refreshed = copy.deepcopy(persisted)
        refreshed["task_id"] = persisted["id"]
        refreshed["task_board"] = {"root": str(self.board)}
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.authorize_command(
                "codex",
                self._command("codex", refreshed),
                self._cwd("codex", refreshed),
                refreshed,
                f"{target}-retry",
            )

    def test_runner_rejects_unmanaged_marker_without_consuming_grant(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        packet.pop("runner_authorization_required")
        with self.assertRaisesRegex(
            RunnerError,
            "permission_grant_runner_context_required",
        ):
            self.state.authorize_command(
                "codex",
                self._command("codex", packet),
                self._cwd("codex", packet),
                packet,
                f"codex-{packet['task_id']}-unmanaged",
            )
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_two_concurrent_authorizations_have_one_winner(self) -> None:
        service, packet, _source, _session = self._grant_packet("hermes")
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def authorize(suffix: str) -> None:
            barrier.wait()
            try:
                self.state.authorize_command(
                    "hermes",
                    self._command("hermes", packet),
                    self._cwd("hermes", packet),
                    packet,
                    f"hermes-{packet['task_id']}-{suffix}",
                )
            except RunnerError:
                outcomes.append("rejected")
            else:
                outcomes.append("authorized")

        threads = [threading.Thread(target=authorize, args=(suffix,)) for suffix in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["authorized", "rejected"])
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"]["uses"], 1)

    def test_packet_drift_wrong_source_unknown_version_and_full_injection_fail(self) -> None:
        _service, packet, _source, _session = self._grant_packet("claude")
        injected = copy.deepcopy(packet)
        injected["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["grant_id"] += "-injected"
        with self.assertRaisesRegex(RunnerError, "permission_authorization_mismatch"):
            self.state.authorize_command(
                "claude",
                self._command("claude", injected),
                self._cwd("claude", injected),
                injected,
                f"claude-{packet['task_id']}-injected",
            )

        service2, wrong_source, _source, _session = self._grant_packet("codex")
        raw = service2.store.read_task(wrong_source["task_id"])
        raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["binding"][
            "source_run_id"
        ] = "codex-wrong-source"
        service2.store.write_task(raw["id"], raw)
        wrong_source = copy.deepcopy(raw)
        wrong_source["task_id"] = raw["id"]
        wrong_source["task_board"] = {"root": str(self.board)}
        wrong_source["runner_authorization_required"] = True
        with self.assertRaisesRegex(RunnerError, "binding"):
            self.state.authorize_command(
                "codex",
                self._command("codex", wrong_source),
                self._cwd("codex", wrong_source),
                wrong_source,
                f"codex-{raw['id']}-wrong-source",
            )

        service3, future, _source, _session = self._grant_packet("hermes")
        raw = service3.store.read_task(future["task_id"])
        raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["version"] = 99
        service3.store.write_task(raw["id"], raw)
        future = copy.deepcopy(raw)
        future["task_id"] = raw["id"]
        future["task_board"] = {"root": str(self.board)}
        future["runner_authorization_required"] = True
        with self.assertRaisesRegex(RunnerError, "version_unsupported"):
            self.state.authorize_command(
                "hermes",
                self._command("hermes", future),
                self._cwd("hermes", future),
                future,
                f"hermes-{raw['id']}-future",
            )

        safe_service = TaskService(
            self.root / "safe-board",
            config={"workspace_root": str(self.root / "safe-workspace")},
        )
        safe_task = safe_service.create_task(
            "raw full injection",
            "hermes",
            [{"id": 1, "description": "reject full flag"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        safe = safe_task.to_dict()
        safe["task_id"] = safe_task.id
        safe["task_board"] = {"root": str(safe_service.board_root)}
        with self.assertRaisesRegex(RunnerError, "do not match"):
            self.state.authorize_command(
                "hermes",
                [str(self.binaries["hermes"]), "chat", "--yolo", "-q", "prompt"],
                str(self.project),
                safe,
                f"hermes-{safe_task.id}-injected",
            )

    def test_runner_submit_uses_preallocated_id_and_spawn_failure_stays_consumed(self) -> None:
        service, packet, _source, _session = self._grant_packet("hermes")
        target = f"hermes-{packet['task_id']}-runner"
        spawned = {"ok": True, "run_id": target, "pid": 42, "status": "running"}
        with mock.patch.object(self.state, "_spawn_process", return_value=spawned) as spawn:
            result = self.state.submit(
                "hermes",
                self._command("hermes", packet),
                self._cwd("hermes", packet),
                packet,
                target,
            )
        self.assertEqual(result["run_id"], target)
        self.assertEqual(spawn.call_args.kwargs["run_id"], target)
        self.assertEqual(
            service.store.read_task(packet["task_id"])["extensions"][
                PERMISSION_GRANT_EXTENSION_KEY
            ]["state"]["status"],
            "consumed",
        )

        service2, packet2, _source, _session = self._grant_packet("claude")
        target2 = f"claude-{packet2['task_id']}-spawn-failure"
        with mock.patch.object(self.state, "_spawn_process", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                self.state.submit(
                    "claude",
                    self._command("claude", packet2),
                    self._cwd("claude", packet2),
                    packet2,
                    target2,
                )
        consumed = service2.store.read_task(packet2["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(consumed["state"]["status"], "consumed")
        self.assertEqual(consumed["binding"]["target_run_id"], target2)

    def test_adapters_build_full_argv_for_same_session_and_pass_target_run(self) -> None:
        cases = (
            ("codex", CodexExecutor(command=str(self.binaries["codex"]))),
            (
                "claude",
                ClaudeExecutor(command=str(self.binaries["claude"]), transport="direct"),
            ),
            (
                "hermes",
                HermesExecutor(command=str(self.binaries["hermes"]), transport="direct"),
            ),
        )
        for executor_name, executor in cases:
            with self.subTest(executor=executor_name):
                _service, packet, _source, session_id = self._grant_packet(executor_name)
                packet["runner_authorization_required"] = True
                if executor_name == "codex":
                    completed = subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=(
                            '{"type":"thread.started","thread_id":"'
                            + session_id
                            + '"}\n'
                        ),
                        stderr="",
                    )
                    authorize_patch = mock.patch(
                        "agent_bridge_connect.executors.codex.RunnerClient.authorize_command",
                        return_value={"ok": True},
                    )
                    run_patch = mock.patch(
                        "agent_bridge_connect.executors.codex.subprocess.run",
                        return_value=completed,
                    )
                elif executor_name == "claude":
                    # PERM-104-002 correction (GGQN-001): a granted full run
                    # routes through the official SDK control transport; the
                    # raw CLI full argv branch no longer exists.  Prove the
                    # authorized command carries the same frozen executor
                    # run id and that the SDK session argv keeps the exact
                    # resume binding instead.
                    completed = subprocess.CompletedProcess(
                        [], 0, stdout='{"type":"result","result":"done"}\n', stderr=""
                    )
                    authorize_patch = mock.patch(
                        "agent_bridge_connect.executors.claude.RunnerClient.authorize_transport",
                        return_value={"ok": True},
                    )
                    authorize_calls: dict[str, object] = {}

                    def _capture_authorize(*args, **kwargs):
                        authorize_calls["executor_run_id"] = kwargs.get("executor_run_id")
                        return {"ok": True}

                    options = object()
                    options_patch = mock.patch.object(
                        executor,
                        "_build_sdk_options_for_task",
                        return_value=options,
                    )
                    run_patch = mock.patch(
                        "agent_bridge_connect.executors.claude.subprocess.run",
                        return_value=completed,
                    )
                    captured: dict[str, object] = {}

                    def _capture_run_controlled(**kwargs):
                        captured["options"] = kwargs.get("options")
                        return {
                            "stdout": '{"type":"result","result":"done"}',
                            "stderr": "",
                            "returncode": 0,
                            "init_verified": True,
                            "session_id": "",
                            "result": {"is_error": False},
                        }

                    transport_patch = mock.patch(
                        "agent_bridge_connect.executors.claude.ClaudeSDKControlTransport.run_controlled",
                        side_effect=_capture_run_controlled,
                    )
                    select_patch = mock.patch(
                        "agent_bridge_connect.executors.claude.select_claude_control_path",
                        return_value="sdk_control_transport",
                    )
                    sdk_env_patch = mock.patch(
                        "agent_bridge_connect.permission_transport.assert_claude_sdk_environment",
                        return_value={"sdk_version": "0.2.142", "platform": "macOS arm64", "cli_path": "/opt/claude"},
                    )
                    authorize_patch = mock.patch(
                        "agent_bridge_connect.executors.claude.RunnerClient.authorize_transport",
                        side_effect=_capture_authorize,
                    )
                    unused = None
                    _ = unused
                else:
                    completed = subprocess.CompletedProcess(
                        [], 0, stdout="done", stderr=f"session_id: {session_id}\n"
                    )
                    authorize_patch = mock.patch.object(
                        executor._runner_client,
                        "authorize_command",
                        return_value={"ok": True},
                    )
                    run_patch = mock.patch(
                        "agent_bridge_connect.executors.hermes.subprocess.run",
                        return_value=completed,
                    )
                with (
                    mock.patch.object(executor, "_start_run_lease"),
                    mock.patch.object(executor, "_heartbeat_run"),
                    mock.patch.object(executor, "_close_run_lease"),
                    mock.patch(
                        f"agent_bridge_connect.executors.{executor_name}.assert_executor_permission_supported"
                    ),
                    authorize_patch as authorize,
                    run_patch as run,
                ):
                    if executor_name == "claude":
                        # Corrected contract: the granted full run is
                        # authorized with the frozen run id and dispatched
                        # into the SDK control transport (no raw CLI argv).
                        with (
                            options_patch,
                            transport_patch,
                            select_patch,
                            sdk_env_patch,
                        ):
                            started = executor.start(packet)
                        self.assertTrue(started.ok, started.message)
                        self.assertEqual(
                            authorize_calls["executor_run_id"], started.run_id
                        )
                        self.assertIs(captured["options"], options)
                        # The control-plane authorize call must never carry a
                        # raw full argv for the SDK transport path.
                        for call in run.call_args_list:
                            self.assertNotIn(
                                permission_flags("claude", "full")[0],
                                call.args[0],
                            )
                        continue
                    started = executor.start(packet)
                self.assertTrue(started.ok, started.message)
                self.assertEqual(
                    authorize.call_args.kwargs["executor_run_id"], started.run_id
                )
                command = next(
                    call.args[0]
                    for call in run.call_args_list
                    if permission_flags(executor_name, "full")[0] in call.args[0]
                )
                self.assertEqual(
                    permission_flags(executor_name, "full")[0] in command,
                    True,
                )
                if executor_name == "codex":
                    self.assertEqual(command[command.index("resume") + 1], session_id)
                else:
                    self.assertEqual(command[command.index("--resume") + 1], session_id)

    def test_hermes_runner_transport_keeps_adapter_run_id(self) -> None:
        _service, packet, _source, session_id = self._grant_packet("hermes")
        executor = HermesExecutor(command=str(self.binaries["hermes"]), transport="runner")
        executor._runner_client.authorize_command = mock.Mock(return_value={"ok": True})
        callback = (
            'AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"'
            + packet["task_id"]
            + '","final_state":"completed","summary":"done",'
            '"step_results":[{"id":1,"status":"done"}]}'
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=callback,
            stderr="",
        )
        with (
            mock.patch.object(executor, "_start_run_lease"),
            mock.patch.object(executor, "_heartbeat_run"),
            mock.patch.object(executor, "_close_run_lease"),
            mock.patch.object(
                executor,
                "_store_run",
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.assert_executor_permission_supported"
            ),
            mock.patch(
                "agent_bridge_connect.executors.hermes.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            started = executor.start(packet)
        self.assertTrue(started.ok, started.message)
        self.assertEqual(
            executor._runner_client.authorize_command.call_args.kwargs[
                "executor_run_id"
            ],
            started.run_id,
        )
        command = next(
            call.args[0]
            for call in run.call_args_list
            if "chat" in call.args[0]
        )
        self.assertIn("--yolo", command)
        self.assertEqual(command[command.index("--resume") + 1], session_id)
        # This packet is already executing inside the Runner-owned worker.
        # The adapter authorizes its exact argv but must not recursively submit
        # another worker, which would lose the preallocated continuation ID.
        self.assertNotIn(started.run_id, executor._runner_runs)

    def test_runner_client_transports_executor_run_id_internally(self) -> None:
        client = RunnerClient(spool_root=self.root / "spool")
        with mock.patch.object(client, "_request", return_value={"ok": True}) as request:
            client.authorize_command(
                "codex",
                ["codex", "exec", "--json"],
                self.project,
                {"task_id": "ABCD-001"},
                executor_run_id="codex-ABCD-001-target",
            )
            authorize_payload = request.call_args.args[0]
            self.assertEqual(
                authorize_payload["executor_run_id"], "codex-ABCD-001-target"
            )
            client.submit(
                "hermes",
                ["hermes", "chat", "-q", "prompt"],
                self.project,
                task={"task_id": "EFGH-001"},
                executor_run_id="hermes-EFGH-001-target",
            )
            submit_payload = request.call_args.args[0]
            self.assertEqual(
                submit_payload["executor_run_id"], "hermes-EFGH-001-target"
            )
            client.authorize_transport(
                "claude",
                CLAUDE_SDK_CONTROL_AUTHORIZATION,
                self.project,
                {"task_id": "SDKC-001"},
                {"control_path": "sdk_control_transport"},
                executor_run_id="claude-SDKC-001-target",
            )
            transport_payload = request.call_args.args[0]
            self.assertEqual(transport_payload["op"], "authorize_transport")
            self.assertEqual(
                transport_payload["transport"], CLAUDE_SDK_CONTROL_AUTHORIZATION
            )
            self.assertEqual(
                transport_payload["executor_run_id"], "claude-SDKC-001-target"
            )
            self.assertEqual(
                transport_payload["context"],
                {"control_path": "sdk_control_transport"},
            )

    def test_runner_authorizes_claude_sdk_transport_without_fake_print_argv(self) -> None:
        _service, packet, _source, session_id = self._grant_packet("claude")
        run_id = f"claude-{packet['task_id']}-sdk"
        permission = resolve_effective_permission(
            packet,
            "claude",
            run_id,
            trusted_runner_managed=True,
        )
        executor = ClaudeExecutor(command=str(self.binaries["claude"]))
        execution_root = Path(self._cwd("claude", packet))
        context = executor._build_sdk_authorization_context(
            packet,
            execution_root,
            session_id,
            {
                "sdk_version": "0.2.142",
                "platform": "macOS arm64",
                "cli_path": str(self.binaries["claude"]),
            },
            permission,
        )

        def _probe(command, *_args, **_kwargs):
            if "--help" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="--dangerously-skip-permissions\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command, 0, stdout="2.1.233 (Claude Code)\n", stderr=""
            )

        with (
            mock.patch(
                "agent_bridge_connect.runner.assert_claude_sdk_environment",
                return_value={
                    "sdk_version": "0.2.142",
                    "platform": "macOS arm64",
                    "cli_path": str(self.binaries["claude"]),
                },
            ),
            mock.patch(
                "agent_bridge_connect.runner.subprocess.run",
                side_effect=_probe,
            ),
        ):
            result = self.state.authorize_transport(
                "claude",
                CLAUDE_SDK_CONTROL_AUTHORIZATION,
                str(execution_root),
                packet,
                context,
                run_id,
            )
        self.assertEqual(result["effective_permission_mode"], "full")
        self.assertEqual(context["permission_mode"], "default")
        self.assertEqual(context["session_mode_update"], "bypassPermissions")

    def test_runner_sdk_transport_rejects_context_drift(self) -> None:
        service = TaskService(
            self.root / "sdk-safe-board",
            config={
                "workspace_root": str(self.root / "sdk-safe-workspace"),
                "executors": {"claude": {"max_budget_usd": 10.0}},
                "sessions": {"retain_executor_sessions": True},
            },
        )
        task = service.create_task(
            "SDK context drift",
            "claude",
            [{"id": 1, "description": "reject drift"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        packet = service.store.read_task(task.id)
        packet["task_id"] = task.id
        packet["task_board"] = {"root": str(service.board_root)}
        packet["runner_authorization_required"] = True
        session = packet["extensions"]["agentbc.session"]
        executor = ClaudeExecutor(command=str(self.binaries["claude"]))
        context = executor._build_sdk_authorization_context(
            packet,
            Path(session["project_path"]),
            session["session_id"],
            {
                "sdk_version": "0.2.142",
                "platform": "macOS arm64",
                "cli_path": str(self.binaries["claude"]),
            },
            resolve_effective_permission(packet, "claude", "claude-sdk-drift"),
        )
        context["permission_mode"] = "bypassPermissions"
        version_probe = subprocess.CompletedProcess(
            [], 0, stdout="2.1.233 (Claude Code)\n", stderr=""
        )
        with (
            mock.patch(
                "agent_bridge_connect.runner.assert_claude_sdk_environment",
                return_value={
                    "sdk_version": "0.2.142",
                    "platform": "macOS arm64",
                    "cli_path": str(self.binaries["claude"]),
                },
            ),
            mock.patch(
                "agent_bridge_connect.runner.subprocess.run",
                return_value=version_probe,
            ),
            self.assertRaisesRegex(RunnerError, "permission_transport_mismatch"),
        ):
            self.state.authorize_transport(
                "claude",
                CLAUDE_SDK_CONTROL_AUTHORIZATION,
                session["project_path"],
                packet,
                context,
                "claude-sdk-drift",
            )

    def test_preconsume_dispatch_failure_calls_core_revoke_helper(self) -> None:
        _service, packet, _source, _session = self._grant_packet("codex")
        task = SimpleNamespace(
            id=packet["task_id"],
            assignee="codex",
            extensions=packet["extensions"],
            to_dict=lambda: packet,
        )
        fake_service = mock.Mock()
        fake_service.respond_to_input.return_value = {
            "dispatch_required": True,
            "input_id": "input-codex-phase6",
        }
        fake_service.expire_waiting_inputs.return_value = []
        fake_service.get_task.return_value = task
        fake_service.board_root = self.board
        with (
            mock.patch("agent_bridge_connect.service.TaskService", return_value=fake_service),
            mock.patch.object(
                self.state,
                "dispatch_worker",
                side_effect=RunnerError("preconsume dispatch failed"),
            ),
            mock.patch("agent_bridge_connect.reports.write_report_files"),
            mock.patch("agent_bridge_connect.notifications.notify_terminal"),
            mock.patch.object(self.state, "_refresh_task_list_dashboard"),
            self.assertRaisesRegex(RunnerError, "preconsume dispatch failed"),
        ):
            self.state.respond_and_dispatch(
                {
                    "board_root": str(self.board),
                    "task_id": packet["task_id"],
                    "input_id": "input-codex-phase6",
                    "response_type": "approve",
                }
            )
        fake_service.revoke_permission_grant.assert_called_once_with(
            packet["task_id"], "dispatch_failed"
        )
        fake_service.mark_task_needs_recovery.assert_called_once()

    def test_consumed_or_foreign_grants_do_not_leak_to_later_flows(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        target = f"codex-{packet['task_id']}-once"
        self.state.authorize_command(
            "codex",
            self._command("codex", packet),
            self._cwd("codex", packet),
            packet,
            target,
        )
        consumed = service.store.read_task(packet["task_id"])
        for status in ("running", "needs_recovery", "pending"):
            with self.subTest(status=status):
                later = copy.deepcopy(consumed)
                later["status"] = status
                self.assertEqual(
                    resolve_effective_permission(
                        later,
                        "codex",
                        f"codex-{packet['task_id']}-{status}",
                    )["effective_mode"],
                    "safe",
                )

        foreign = copy.deepcopy(packet)
        foreign["task_id"] = "NEXT-001"
        foreign["id"] = "NEXT-001"
        with self.assertRaises(ABCError):
            resolve_effective_permission(foreign, "codex", "codex-NEXT-001-new")

        raw = service.store.read_task(packet["task_id"])
        raw["status"] = "completed"
        service.store.write_task(raw["id"], raw)
        handoff = service.handoff_task(raw["id"], "codex", "continue safely")
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, handoff.extensions)
        new_task = service.create_task(
            "new task",
            "codex",
            [{"id": 1, "description": "fresh safe run"}],
            customer_dir=True,
            customer_path=self.project,
            permission_mode="safe",
        )
        self.assertNotIn(PERMISSION_GRANT_EXTENSION_KEY, new_task.extensions)


if __name__ == "__main__":
    unittest.main()
