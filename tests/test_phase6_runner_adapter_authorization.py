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
from agent_bridge_connect.permission_modes import PERMISSION_EXTENSION_KEY, build_permission_record
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

    def test_issued_grant_is_read_only_and_does_not_change_safe_dispatch(self) -> None:
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
        self.assertIsNone(kwargs.get("containment"))
        self.assertNotIn(
            "agentbc.permission_runtime",
            service.get_task(task_id).extensions or {},
        )
        # The historical grant remains persisted for readers, but it cannot
        # turn a safe task into a full worker or create a runtime receipt.
        grant = service.get_task(task_id).extensions[PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"]["status"], "issued")

    def test_common_resolver_keeps_legacy_grants_read_only(self) -> None:
        for executor in ("codex", "claude", "hermes"):
            with self.subTest(executor=executor):
                _service, packet, _source_run_id, session_id = self._grant_packet(
                    executor
                )
                target = f"{executor}-{packet['task_id']}-target"
                resolved = resolve_effective_permission(
                    packet,
                    executor,
                    target,
                    trusted_runner_managed=True,
                )
                self.assertEqual(resolved["effective_mode"], "safe")
                self.assertEqual(
                    packet["extensions"]["agentbc.session"]["session_id"],
                    session_id,
                )

        _service, hermes, _source, _session = self._grant_packet("hermes")
        hermes["extensions"]["agentbc.session"]["session_id"] = ""
        self.assertEqual(
            resolve_effective_permission(
                hermes,
                "hermes",
                "hermes-target",
                trusted_runner_managed=True,
            )["effective_mode"],
            "safe",
        )

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

    def _assert_unmanaged_adapter_keeps_legacy_grant_inert(self, executor_name: str) -> None:
        service, packet, _source, _session = self._grant_packet(executor_name)
        resolved = resolve_effective_permission(
            packet,
            executor_name,
            f"{executor_name}-{packet['task_id']}-unmanaged",
        )
        self.assertEqual(resolved["effective_mode"], "safe")
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_codex_unmanaged_issued_grant_is_inert(self) -> None:
        self._assert_unmanaged_adapter_keeps_legacy_grant_inert("codex")

    def test_claude_unmanaged_issued_grant_is_inert(self) -> None:
        self._assert_unmanaged_adapter_keeps_legacy_grant_inert("claude")

    def test_hermes_unmanaged_issued_grant_is_inert(self) -> None:
        self._assert_unmanaged_adapter_keeps_legacy_grant_inert("hermes")

    def test_runner_authorization_keeps_legacy_grant_read_only(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        target = f"codex-{packet['task_id']}-safe"
        with mock.patch(
            "agent_bridge_connect.runner.resolve_effective_permission",
            wraps=resolve_effective_permission,
        ) as resolver:
            result = self.state.authorize_command(
                "codex",
                self._command("codex", packet, full=False),
                self._cwd("codex", packet),
                packet,
                target,
            )
        self.assertEqual(result["effective_permission_mode"], "safe")
        self.assertFalse(resolver.call_args.kwargs["trusted_runner_managed"])
        persisted = service.store.read_task(packet["task_id"])
        grant = persisted["extensions"][PERMISSION_GRANT_EXTENSION_KEY]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_runner_allows_safe_authorization_without_legacy_grant_context(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        packet.pop("runner_authorization_required")
        result = self.state.authorize_command(
            "codex",
            self._command("codex", packet, full=False),
            self._cwd("codex", packet),
            packet,
            f"codex-{packet['task_id']}-unmanaged",
        )
        self.assertEqual(result["effective_permission_mode"], "safe")
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})
        self.assertEqual(grant["binding"]["target_run_id"], "")

    def test_two_concurrent_authorizations_do_not_consume_legacy_grant(self) -> None:
        service, packet, _source, _session = self._grant_packet("hermes")
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def authorize(suffix: str) -> None:
            barrier.wait()
            try:
                self.state.authorize_command(
                    "hermes",
                    self._command("hermes", packet, full=False),
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
        self.assertCountEqual(outcomes, ["authorized", "authorized"])
        grant = service.store.read_task(packet["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(grant["state"], {"status": "issued", "uses": 0})

    def test_packet_drift_and_raw_full_injection_still_fail_closed(self) -> None:
        _service, packet, _source, _session = self._grant_packet("claude")
        injected = copy.deepcopy(packet)
        injected["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["grant_id"] += "-injected"
        with self.assertRaisesRegex(RunnerError, "permission_authorization_mismatch"):
            self.state.authorize_command(
                "claude",
                self._command("claude", injected, full=False),
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
        result = self.state.authorize_command(
            "codex",
            self._command("codex", wrong_source, full=False),
            self._cwd("codex", wrong_source),
            wrong_source,
            f"codex-{raw['id']}-wrong-source",
        )
        self.assertEqual(result["effective_permission_mode"], "safe")

        service3, future, _source, _session = self._grant_packet("hermes")
        raw = service3.store.read_task(future["task_id"])
        raw["extensions"][PERMISSION_GRANT_EXTENSION_KEY]["version"] = 99
        service3.store.write_task(raw["id"], raw)
        future = copy.deepcopy(raw)
        future["task_id"] = raw["id"]
        future["task_board"] = {"root": str(self.board)}
        future["runner_authorization_required"] = True
        result = self.state.authorize_command(
            "hermes",
            self._command("hermes", future, full=False),
            self._cwd("hermes", future),
            future,
            f"hermes-{raw['id']}-future",
        )
        self.assertEqual(result["effective_permission_mode"], "safe")

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

    def test_runner_submit_keeps_legacy_grant_issued_on_dispatch(self) -> None:
        service, packet, _source, _session = self._grant_packet("hermes")
        target = f"hermes-{packet['task_id']}-runner"
        spawned = {"ok": True, "run_id": target, "pid": 42, "status": "running"}
        with mock.patch.object(self.state, "_spawn_process", return_value=spawned) as spawn:
            result = self.state.submit(
                "hermes",
                self._command("hermes", packet, full=False),
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
            "issued",
        )

        service2, packet2, _source, _session = self._grant_packet("claude")
        target2 = f"claude-{packet2['task_id']}-spawn-failure"
        with mock.patch.object(self.state, "_spawn_process", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                self.state.submit(
                    "claude",
                    self._command("claude", packet2, full=False),
                    self._cwd("claude", packet2),
                    packet2,
                    target2,
                )
        retained = service2.store.read_task(packet2["task_id"])["extensions"][
            PERMISSION_GRANT_EXTENSION_KEY
        ]
        self.assertEqual(retained["state"], {"status": "issued", "uses": 0})
        self.assertEqual(retained["binding"]["target_run_id"], "")

    def test_explicit_full_adapters_use_native_flags_not_legacy_grants(self) -> None:
        cases = ("codex", "claude", "hermes")
        for executor_name in cases:
            with self.subTest(executor=executor_name):
                service, packet, _source, _session = self._grant_packet(executor_name)
                raw = service.store.read_task(packet["task_id"])
                raw["extensions"][PERMISSION_EXTENSION_KEY] = build_permission_record(
                    explicit_mode="full"
                )
                service.store.write_task(raw["id"], raw)
                packet = copy.deepcopy(raw)
                packet["task_id"] = raw["id"]
                packet["task_board"] = {"root": str(self.board)}
                permission = resolve_effective_permission(
                    packet, executor_name, f"{executor_name}-{raw['id']}-full"
                )
                self.assertEqual(permission["effective_mode"], "full")
                if executor_name == "codex":
                    command, _ = CodexExecutor(
                        command=str(self.binaries[executor_name]), transport="direct"
                    )._build_command(packet, "prompt", self.project, permission)
                elif executor_name == "claude":
                    command = ClaudeExecutor(
                        command=str(self.binaries[executor_name]), transport="direct"
                    )._build_command("prompt", self.project, packet, permission)
                else:
                    command = HermesExecutor(
                        command=str(self.binaries[executor_name]), transport="direct"
                    )._build_command("prompt", permission=permission, task_packet=packet)
                self.assertIn(permission_flags(executor_name, "full")[0], command)
                self.assertIn(PERMISSION_GRANT_EXTENSION_KEY, packet["extensions"])

    def test_hermes_runner_transport_keeps_adapter_run_id(self) -> None:
        service, packet, _source, session_id = self._grant_packet("hermes")
        raw = service.store.read_task(packet["task_id"])
        raw["extensions"][PERMISSION_EXTENSION_KEY] = build_permission_record(
            explicit_mode="full"
        )
        service.store.write_task(raw["id"], raw)
        packet = copy.deepcopy(raw)
        packet["task_id"] = raw["id"]
        packet["task_board"] = {"root": str(self.board)}
        packet["runner_authorization_required"] = True
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

    def test_runner_keeps_claude_sdk_transport_safe_with_legacy_grant(self) -> None:
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
        self.assertEqual(result["effective_permission_mode"], "safe")
        self.assertEqual(context["permission_mode"], "default")
        self.assertEqual(context["session_mode_update"], "")

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

    def test_preconsume_dispatch_failure_does_not_revoke_legacy_grant(self) -> None:
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
        fake_service.revoke_permission_grant.assert_not_called()
        fake_service.mark_task_needs_recovery.assert_called_once()

    def test_legacy_grants_do_not_leak_to_later_flows(self) -> None:
        service, packet, _source, _session = self._grant_packet("codex")
        target = f"codex-{packet['task_id']}-once"
        self.state.authorize_command(
            "codex",
            self._command("codex", packet, full=False),
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
        self.assertEqual(
            resolve_effective_permission(
                foreign, "codex", "codex-NEXT-001-new"
            )["effective_mode"],
            "safe",
        )

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
