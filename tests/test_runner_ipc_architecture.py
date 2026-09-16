"""ARCH-104-001 Slice B architecture coverage for the Runner IPC layer.

Slice B moved the Runner IPC transport out of ``agent_bridge_connect.runner``:

* ``agent_bridge_connect.runner_contract`` owns ``RunnerError``, the IPC size and
  channel constants, the default Runner path contracts and the pid/endpoint
  identity primitives.
* ``agent_bridge_connect.runner_ipc`` owns ``RunnerClient``, ``RunnerService``,
  request authentication/expiry/serialization, response handling and the one
  ``_dispatch_request`` routing chain.
* ``agent_bridge_connect.runner`` keeps RunnerState, process
  authorization/spawn/reap, task dispatch, maintenance and
  ``create_runner_service``, and re-exports the moved names unchanged.

Contracts under test:

* Every name that used to be importable from ``agent_bridge_connect.runner`` is
  still importable and is the *same object* (no forwarding wrapper).
* There is exactly one router and exactly one handler per operation; the
  production flow stays ``RunnerClient -> _dispatch_request -> RunnerState``.
* Token authentication, channel validation, request expiry and the request size
  limit are enforced by the service with their exact error text.
* Registration state survives a service restart on the same spool.
* ``runner_ipc`` and ``runner_contract`` import cleanly without pulling in
  ``runner`` -- zero circular imports.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_bridge_connect import runner as runner_module  # noqa: E402
from agent_bridge_connect import runner_contract, runner_ipc  # noqa: E402
from agent_bridge_connect.runner import (  # noqa: E402
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    RunnerClient,
    RunnerError,
    RunnerService,
    RunnerState,
    _dispatch_request,
)

# The frozen Slice B routing contract: operation -> the single RunnerState
# handler the router must invoke.  ``health`` is answered inline by the router
# and reaches no handler at all.
OP_HANDLERS: dict[str, str | None] = {
    "health": None,
    "storage_status": "storage_status",
    "submit": "submit",
    "authorize_command": "authorize_command",
    "authorize_transport": "authorize_transport",
    "respond_approval": "respond_approval",
    "control_status": "control_status",
    "control_events": "control_events",
    "process_sample": "process_sample",
    "dispatch_worker": "dispatch_worker",
    "dispatch_task": "dispatch_task",
    "respond_task": "respond_and_dispatch",
    "create_and_dispatch": "create_and_dispatch",
    "handoff_and_dispatch": "handoff_and_dispatch",
    "register_desktop_route": "register_desktop_route",
    "acknowledge_desktop_archive": "acknowledge_desktop_archive",
    "status": "status",
    "cancel": "cancel",
    "cancel_task_runs": "cancel_task_runs",
    "write_report": "write_report",
    "terminal_delivery": "terminal_delivery",
    "agent_callback": "agent_callback",
    "show_task": "show_task",
}

# Names that moved out of runner.py and must stay importable from it.
MOVED_FROM_CONTRACT = (
    "MAX_REQUEST_BYTES",
    "MAX_OUTPUT_BYTES",
    "RUNNER_IDENTITY_REFRESH_INTERVAL_S",
    "RUNNER_IPC_CHANNEL_RE",
    "RunnerError",
    "default_runner_root",
    "default_runner_spool",
    "default_runner_token",
    "default_runner_log",
    "_read_runner_pid",
    "_pid_is_alive",
)
MOVED_FROM_IPC = (
    "RunnerClient",
    "RunnerService",
    "_dispatch_request",
    "_load_or_create_token",
)


class _StubDesktopArchiveBroker:
    """Minimal stand-in for the Desktop archive broker on the health path."""

    def public_status(self) -> dict[str, object]:
        return {"status": "not_registered"}


class _RecordingOperations:
    """Structural ``RunnerOperations`` stand-in recording handler calls.

    ``_dispatch_request`` is typed against the internal protocol rather than
    ``RunnerState``, so any object exposing the same surface must drive it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.allowed_executables = {"hermes": Path("/bin/echo")}
        self.executable_sources = {"hermes": "config"}
        self.desktop_archive_broker = _StubDesktopArchiveBroker()

    def _record(self, name: str, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((name, args, kwargs))
        return {"ok": True, "handler": name}

    def __getattr__(self, name: str):
        if name in set(OP_HANDLERS.values()):
            return lambda *args, **kwargs: self._record(name, *args, **kwargs)
        raise AttributeError(name)


class RunnerIpcHarness(unittest.TestCase):
    """Shared real ``RunnerState`` + ``RunnerService`` + ``RunnerClient`` fixture."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_hermes = self.root / "hermes"
        self.fake_hermes.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "sleep" ]; then sleep 30; fi\n'
            "printf 'RUNNER_OK'\n",
            encoding="utf-8",
        )
        self.fake_hermes.chmod(self.fake_hermes.stat().st_mode | stat.S_IXUSR)
        self.fake_claude = self.root / "claude"
        self.fake_claude.write_text("#!/bin/sh\nprintf 'CLAUDE_OK'\n", encoding="utf-8")
        self.fake_claude.chmod(self.fake_claude.stat().st_mode | stat.S_IXUSR)
        self.state = self._new_state()
        self.spool = self.root / "spool"
        self.token = self.spool / "token"
        self.service: RunnerService | None = None
        self.thread: threading.Thread | None = None

    def tearDown(self) -> None:
        self._stop_service()
        self.temp.cleanup()

    def _new_state(self) -> RunnerState:
        return RunnerState(
            self.root / "state",
            [self.root],
            {"hermes": self.fake_hermes, "claude": self.fake_claude},
        )

    def _stop_service(self) -> None:
        if self.service is not None:
            self.service.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        self.service = None
        self.thread = None

    def start_service(self, *, spool: Path | None = None, state: RunnerState | None = None) -> RunnerClient:
        spool = spool or self.spool
        service = RunnerService(spool, spool / "token", state or self.state, interval_s=0.01)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.service = service
        self.thread = thread
        return RunnerClient(spool, spool / "token", timeout_s=5)

    def _queue_request(
        self,
        payload: object,
        *,
        channel: str = "",
        authenticate: bool = True,
    ) -> str:
        """Write one raw request file and return its request id."""
        requests_dir = self.spool / "requests" / channel if channel else self.spool / "requests"
        request_id = uuid.uuid4().hex
        request = payload
        if isinstance(payload, dict):
            envelope: dict[str, object] = {"request_id": request_id}
            if authenticate:
                envelope["token"] = self.token.read_text(encoding="utf-8").strip()
                envelope["expires_at"] = time.time() + 30
            envelope.update(payload)
            request = envelope
        (requests_dir / f"{request_id}.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        return request_id

    def _write_request(
        self,
        payload: object,
        *,
        channel: str = "",
        authenticate: bool = True,
    ) -> dict[str, object]:
        """Write one raw request file and return the service's raw response.

        ``authenticate`` injects the live token and a future expiry so a test can
        exercise a handler rather than the authentication gate; pass ``False`` to
        hand-craft the credential envelope (foreign token, missing token).
        """
        responses_dir = self.spool / "responses" / channel if channel else self.spool / "responses"
        request_id = self._queue_request(
            payload, channel=channel, authenticate=authenticate
        )
        response_path = responses_dir / f"{request_id}.json"
        for _ in range(500):
            if response_path.exists():
                result = json.loads(response_path.read_text(encoding="utf-8"))
                response_path.unlink(missing_ok=True)
                return result
            time.sleep(0.01)
        self.fail("runner service did not answer the raw request")


class RunnerIpcPublicSurfaceTests(unittest.TestCase):
    """The compatibility facade must preserve identity, not forward calls."""

    def test_moved_names_stay_importable_from_runner_by_identity(self) -> None:
        for name in MOVED_FROM_CONTRACT:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(runner_module, name),
                    getattr(runner_contract, name),
                )
                self.assertIn(name, runner_module.__dict__)
        for name in MOVED_FROM_IPC:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(runner_module, name),
                    getattr(runner_ipc, name),
                )

    def test_runner_error_is_the_single_shared_type(self) -> None:
        self.assertIs(runner_module.RunnerError, runner_ipc.RunnerError)
        self.assertIs(runner_module.RunnerError, runner_contract.RunnerError)
        self.assertTrue(issubclass(RunnerError, RuntimeError))

    def test_no_runtime_forwarding_wrapper_for_moved_names(self) -> None:
        """The facade re-exports; it must not define forwarding stand-ins."""
        self.assertIs(RunnerClient, runner_ipc.RunnerClient)
        self.assertIs(RunnerService, runner_ipc.RunnerService)
        self.assertIs(_dispatch_request, runner_ipc._dispatch_request)
        for name, owner in [
            *((name, runner_ipc) for name in MOVED_FROM_IPC),
            *((name, runner_contract) for name in MOVED_FROM_CONTRACT),
        ]:
            with self.subTest(name=name):
                value = getattr(runner_module, name)
                # Object identity is the discriminator: a runner.py forwarding
                # stand-in would be a distinct object.
                self.assertIs(
                    value,
                    getattr(owner, name),
                    f"{name} must be the object {owner.__name__} defines",
                )
                # ``__module__`` names a defining module only for objects Python
                # attributes to one, so scope that extra check to the shapes a
                # forwarding wrapper could actually take.  Constants are
                # attributed to their type's module instead -- a compiled pattern
                # reports ``re``, an int reports ``builtins``.
                if not (inspect.isclass(value) or inspect.isfunction(value)):
                    continue
                self.assertEqual(
                    value.__module__,
                    owner.__name__,
                    f"{name} must be defined in {owner.__name__}, not runner.py",
                )

    def test_default_path_contracts_are_unchanged(self) -> None:
        with mock.patch.dict(
            os.environ, {"AGENTBC_RUNNER_SPOOL": str(self.root_spool())}
        ):
            self.assertEqual(
                runner_contract.default_runner_spool(), self.root_spool()
            )
            self.assertEqual(
                runner_contract.default_runner_token(), self.root_spool() / "token"
            )
        self.assertEqual(
            runner_contract.default_runner_root(), Path.home() / ".abc" / "runner"
        )
        self.assertEqual(
            runner_contract.default_runner_log(),
            runner_contract.default_runner_root() / "runner.log",
        )

    def test_ipc_size_constants_are_shared(self) -> None:
        self.assertEqual(MAX_REQUEST_BYTES, 1024 * 1024)
        self.assertEqual(MAX_OUTPUT_BYTES, 1024 * 1024)
        self.assertEqual(runner_module.MAX_REQUEST_BYTES, MAX_REQUEST_BYTES)
        self.assertEqual(runner_contract.MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES)

    def test_channel_pattern_is_shared_with_the_process_layer(self) -> None:
        pattern = runner_contract.RUNNER_IPC_CHANNEL_RE
        self.assertIs(runner_module.RUNNER_IPC_CHANNEL_RE, pattern)
        self.assertEqual(pattern.pattern, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        self.assertTrue(pattern.fullmatch("runner-worker-private-1"))
        self.assertIsNone(pattern.fullmatch("bad channel"))

    @staticmethod
    def root_spool() -> Path:
        return Path("/tmp") / "agentbc-runner-v2-architecture-test"


class RunnerIpcImportGraphTests(unittest.TestCase):
    """The IPC layer must not depend on the state layer."""

    def _import_probe(self, module: str) -> set[str]:
        probe = (
            "import sys;"
            f"import {module};"
            "print(sorted(m for m in sys.modules if m.startswith('agent_bridge_connect')))"
        )
        env = dict(os.environ, PYTHONPATH=str(SRC_ROOT))
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return set(eval(completed.stdout.strip()))

    def test_runner_ipc_does_not_import_the_runner_state_module(self) -> None:
        loaded = self._import_probe("agent_bridge_connect.runner_ipc")
        self.assertIn("agent_bridge_connect.runner_ipc", loaded)
        self.assertNotIn("agent_bridge_connect.runner", loaded)

    def test_runner_contract_imports_neither_runner_layer(self) -> None:
        loaded = self._import_probe("agent_bridge_connect.runner_contract")
        self.assertIn("agent_bridge_connect.runner_contract", loaded)
        self.assertNotIn("agent_bridge_connect.runner", loaded)
        self.assertNotIn("agent_bridge_connect.runner_ipc", loaded)

    def test_runner_state_is_not_a_protocol_subclass(self) -> None:
        """RunnerOperations stays structural; RunnerState must not inherit it."""
        operations = runner_ipc.RunnerOperations
        self.assertTrue(getattr(operations, "_is_protocol", False))
        self.assertNotIn(operations, RunnerState.__mro__)
        self.assertNotIn("RunnerState", dir(runner_ipc))


class RunnerIpcRouterTests(unittest.TestCase):
    """One router, one handler per operation."""

    def setUp(self) -> None:
        self.source = runner_ipc._dispatch_request.__code__
        self.module_source = Path(runner_ipc.__file__).read_text(encoding="utf-8")

    def test_router_handles_exactly_the_frozen_operation_set(self) -> None:
        routed = re.findall(r'operation == "([a-z_]+)"', self.module_source)
        self.assertEqual(sorted(routed), sorted(OP_HANDLERS))

    def test_there_is_exactly_one_request_router_in_the_package(self) -> None:
        marker = "unknown runner operation: "
        carriers = []
        for path in sorted((SRC_ROOT / "agent_bridge_connect").glob("*.py")):
            if marker in path.read_text(encoding="utf-8"):
                carriers.append(path.name)
        self.assertEqual(carriers, ["runner_ipc.py"])

    def test_every_operation_reaches_exactly_one_handler(self) -> None:
        for operation, expected in OP_HANDLERS.items():
            with self.subTest(operation=operation):
                state = _RecordingOperations()
                _dispatch_request(state, {"op": operation})
                if expected is None:
                    self.assertEqual(state.calls, [])
                else:
                    self.assertEqual([call[0] for call in state.calls], [expected])

    def test_unknown_operation_raises_the_exact_error(self) -> None:
        with self.assertRaisesRegex(
            RunnerError, r"^unknown runner operation: not_an_operation$"
        ):
            _dispatch_request(_RecordingOperations(), {"op": "not_an_operation"})

    def test_missing_operation_raises_the_exact_error(self) -> None:
        with self.assertRaisesRegex(RunnerError, r"^unknown runner operation: $"):
            _dispatch_request(_RecordingOperations(), {})

    def test_all_declared_handlers_exist_on_runner_state_and_the_protocol(self) -> None:
        handlers = {name for name in OP_HANDLERS.values() if name is not None}
        for handler in sorted(handlers):
            with self.subTest(handler=handler):
                self.assertTrue(callable(getattr(RunnerState, handler, None)))
                self.assertIn(handler, dir(runner_ipc.RunnerOperations))

    def test_protocol_declares_the_maintenance_surface_the_service_drives(self) -> None:
        for handler in (
            "maintain_waiting_inputs",
            "maintain_terminal_delivery",
            "maintain_session_cleanup",
        ):
            with self.subTest(handler=handler):
                self.assertIn(handler, dir(runner_ipc.RunnerOperations))
                self.assertTrue(callable(getattr(RunnerState, handler, None)))


class RunnerIpcAuthenticationTests(RunnerIpcHarness):
    """Token authentication, expiry, channel validation and size limits."""

    def test_request_with_a_foreign_token_is_rejected(self) -> None:
        self.start_service()
        response = self._write_request(
            {
                "op": "health",
                "token": "not-the-runner-token",
                "expires_at": time.time() + 30,
            },
            authenticate=False,
        )
        self.assertIs(response["ok"], False)
        self.assertRegex(
            str(response["error"]), r"^runner authentication failed \(runner pid \d+\)$"
        )

    def test_request_without_a_token_is_rejected(self) -> None:
        self.start_service()
        response = self._write_request(
            {"op": "health", "expires_at": time.time() + 30},
            authenticate=False,
        )
        self.assertIs(response["ok"], False)
        self.assertIn("runner authentication failed", str(response["error"]))

    def test_expired_request_is_rejected_before_authentication(self) -> None:
        self.start_service()
        token = self.token.read_text(encoding="utf-8").strip()
        response = self._write_request(
            {"op": "health", "token": token, "expires_at": time.time() - 1}
        )
        self.assertIs(response["ok"], False)
        self.assertEqual(response["error"], "runner request expired")

    def test_non_object_request_is_rejected(self) -> None:
        self.start_service()
        response = self._write_request(["not", "an", "object"])  # type: ignore[arg-type]
        self.assertIs(response["ok"], False)
        self.assertEqual(response["error"], "runner request must be an object")

    def test_client_rejects_an_invalid_channel_before_any_ipc(self) -> None:
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_CHANNEL": "bad channel!"}):
            with self.assertRaisesRegex(
                RunnerError, "^runner IPC channel is invalid$"
            ):
                RunnerClient(spool_root=self.root / "channel-spool")

    def test_client_accepts_a_contained_channel_name(self) -> None:
        channel = "runner-worker-private-1"
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_CHANNEL": channel}):
            client = RunnerClient(spool_root=self.root / "channel-spool")
        self.assertEqual(client.channel, channel)

    def test_service_ignores_a_request_directory_that_is_not_a_valid_channel(self) -> None:
        self.start_service()
        stray = self.spool / "requests" / "bad channel!"
        stray.mkdir(parents=True)
        stray_request = stray / "stray.json"
        stray_request.write_text("{}", encoding="utf-8")
        time.sleep(0.1)
        self.assertTrue(stray_request.exists(), "invalid channel roots must be skipped")
        self.assertFalse((self.spool / "responses" / "bad channel!").exists())
        self.assertFalse((self.spool / "processing" / "bad channel!").exists())

    def test_client_reports_a_missing_token_file(self) -> None:
        self.start_service()
        missing = self.root / "absent-spool"
        (missing / "requests").mkdir(parents=True)
        (missing / "responses").mkdir(parents=True)
        client = RunnerClient(missing, missing / "token", timeout_s=1.0)
        with self.assertRaisesRegex(RunnerError, "^runner token unavailable: "):
            client.health()

    def test_client_reports_an_unavailable_spool(self) -> None:
        absent = self.root / "no-such-spool"
        absent.mkdir()
        (absent / "token").write_text("t\n", encoding="utf-8")
        client = RunnerClient(absent, absent / "token", timeout_s=1.0)
        with self.assertRaisesRegex(RunnerError, "^runner spool is unavailable$"):
            client.health()

    def test_request_over_the_size_limit_is_rejected_by_the_client(self) -> None:
        self.start_service()
        client = RunnerClient(self.spool, self.token, timeout_s=2.0)
        oversized = "x" * (MAX_REQUEST_BYTES + 1)
        with self.assertRaisesRegex(
            RunnerError, "^runner request exceeds size limit$"
        ):
            client.write_report(self.root / "ABCD-001-report.md", oversized)

    def test_request_just_under_the_size_limit_is_accepted(self) -> None:
        self.start_service()
        client = RunnerClient(self.spool, self.token, timeout_s=5.0)
        target = self.root / "tasks" / "artifacts" / "ABCD-001-report.md"
        content = "y" * 4096
        result = client.write_report(target, content)
        self.assertIs(result["ok"], True)
        self.assertEqual(result["bytes"], len(content.encode("utf-8")))

    def test_output_limit_truncates_managed_output_at_the_shared_constant(self) -> None:
        from agent_bridge_connect.runner import _read_output

        path = self.root / "big-output.txt"
        path.write_text("z" * 4096, encoding="utf-8")
        with mock.patch.object(runner_module, "MAX_OUTPUT_BYTES", 16):
            payload, truncated = _read_output(path)
        self.assertTrue(truncated)
        self.assertEqual(payload, "z" * 16)


class RunnerIpcErrorPropagationTests(RunnerIpcHarness):
    """Handler errors must cross the IPC boundary with their exact text."""

    def test_domain_error_text_is_propagated_unchanged(self) -> None:
        client = self.start_service()
        sentinel = "runner storage probe requires between 1 and 8 paths"
        with mock.patch.object(
            self.state, "storage_status", side_effect=RunnerError(sentinel)
        ):
            with self.assertRaises(RunnerError) as caught:
                client.storage_status([self.root])
        self.assertEqual(str(caught.exception), sentinel)

    def test_abc_error_is_propagated_unchanged(self) -> None:
        from agent_bridge_connect.protocol import ABCError

        client = self.start_service()
        with mock.patch.object(
            self.state, "show_task", side_effect=ABCError("board_locked", "board is locked")
        ):
            with self.assertRaises(RunnerError) as caught:
                client.show_task("ABCD-001", self.root)
        self.assertEqual(str(caught.exception), "board is locked")

    def test_only_declared_error_types_are_contained_by_the_service(self) -> None:
        """An undeclared exception escapes serve_once instead of becoming a response."""
        self.start_service()
        self._stop_service()
        service = RunnerService(self.spool, self.token, self.state, interval_s=0.01)
        try:
            self._queue_request(
                {"op": "show_task", "task_id": "ABCD-001", "board_root": str(self.root)}
            )
            with mock.patch.object(
                self.state, "show_task", side_effect=KeyError("boom")
            ):
                with self.assertRaises(KeyError):
                    service.serve_once()
        finally:
            service.shutdown()

    def test_business_error_does_not_stop_the_service(self) -> None:
        client = self.start_service()
        token = self.token.read_text(encoding="utf-8").strip()
        self.assertTrue(token)
        with self.assertRaises(RunnerError):
            client.status("no-such-run")
        self.assertEqual(client.health()["status"], "ready")
        self.assertTrue(self.thread is not None and self.thread.is_alive())


class RunnerIpcRoundTripTests(RunnerIpcHarness):
    """Real client -> service -> state round trips for the named operations."""

    def test_status_and_cancel_round_trip_one_live_run(self) -> None:
        client = self.start_service()
        run = self.state._spawn_process(
            "worker:hermes",
            [str(self.fake_hermes), "sleep"],
            self.root,
            "runner-arch-status",
        )
        run_id = run["run_id"]
        try:
            status = client.status(run_id)
            self.assertEqual(status["status"], "running")
            cancelled = client.cancel(run_id)
            self.assertIs(cancelled["ok"], True)
            self.assertEqual(cancelled["run_id"], run_id)
        finally:
            self.state.cancel(run_id)

    def test_status_unknown_run_keeps_the_exact_error(self) -> None:
        client = self.start_service()
        with self.assertRaisesRegex(RunnerError, r"^unknown runner run: ABCD$"):
            client.status("ABCD")

    def test_create_and_dispatch_reaches_the_state_handler_over_ipc(self) -> None:
        client = self.start_service()
        config_path = self.root / "config.toml"
        config_path.write_text(f'workspace_root = "{self.root}"\n', encoding="utf-8")
        workspace = self.root / "ws"
        workspace.mkdir(exist_ok=True)
        board = self.root / "board"
        dispatched = {
            "run_id": "runner-worker-arch-create",
            "pid": 1234,
            "status": "running",
            "dispatch_status": "accepted",
            "monitor_status": "disabled",
        }
        with (
            mock.patch.dict(
                os.environ, {"AGENTBC_CONFIG_PATH": str(config_path)}, clear=False
            ),
            mock.patch.object(
                self.state, "dispatch_worker", return_value=dict(dispatched)
            ) as handler,
        ):
            response = self._write_request(
                {
                    "op": "create_and_dispatch",
                    "title": "Architecture slice B",
                    "assignee": "hermes",
                    "steps": [{"id": 1, "description": "run"}],
                    "board_root": str(board),
                    "config_path": "",
                    "customer_dir": True,
                    "customer_path": str(workspace),
                    "interval_s": 2.0,
                    "monitor": False,
                }
            )
        self.assertTrue(handler.called)
        self.assertEqual(response["dispatch_status"], "accepted")
        self.assertRegex(str(response["task_id"]), r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}-001$")
        self.assertEqual(client.health()["status"], "ready")

    def test_respond_task_routes_to_respond_and_dispatch_over_ipc(self) -> None:
        self.start_service()
        with mock.patch.object(
            self.state,
            "respond_and_dispatch",
            return_value={"ok": True, "resumed": True},
        ) as handler:
            response = self._write_request(
                {
                    "op": "respond_task",
                    "task_id": "ABCD-001",
                    "input_id": "input-1",
                    "response_type": "message",
                    "message": "continue",
                    "board_root": str(self.root),
                    "config_path": "",
                    "interval_s": 2.0,
                }
            )
        self.assertIs(response["ok"], True)
        self.assertTrue(handler.called)
        self.assertEqual(handler.call_args.args[0]["op"], "respond_task")

    def test_handoff_keeps_the_exact_domain_error_over_ipc(self) -> None:
        from agent_bridge_connect.service import TaskService

        client = self.start_service()
        board = self.root / "handoff-board"
        workspace = self.root / "handoff-workspace"
        workspace.mkdir()
        service = TaskService(board, config={"workspace_root": str(self.root)})
        source = service.create_task(
            "Failed source",
            "hermes",
            [{"id": 1, "description": "source"}],
            customer_dir=True,
            customer_path=workspace,
        )
        service.start_task_run(source.id, "hermes")
        service.mark_task_failed(source.id, "test_failure", "cannot hand off")
        service.mark_task_needs_recovery(source.id, "test_failure", "retry not allowed")
        service.cancel_task(source.id)
        with self.assertRaisesRegex(RunnerError, "handoff requires"):
            client.handoff_and_dispatch(
                source.id, "hermes", "continue", board, None, source_platform="codex"
            )

    def test_terminal_delivery_keeps_the_exact_domain_error_over_ipc(self) -> None:
        client = self.start_service()
        outside = Path(tempfile.gettempdir()) / "agentbc-arch-outside-board"
        outside.mkdir(exist_ok=True)
        with self.assertRaisesRegex(
            RunnerError, "^task board is outside allowed roots: "
        ):
            client.deliver_terminal("ABCD-001", outside)
        with mock.patch.object(
            self.state,
            "terminal_delivery",
            side_effect=RunnerError("terminal delivery failed: sentinel"),
        ):
            with self.assertRaises(RunnerError) as caught:
                client.deliver_terminal("ABCD-001", self.root)
        self.assertEqual(str(caught.exception), "terminal delivery failed: sentinel")

    def test_desktop_route_registration_is_reachable_over_ipc(self) -> None:
        from agent_bridge_connect.codex_desktop_archive import CodexDesktopRouteContext

        client = self.start_service()
        incomplete = CodexDesktopRouteContext(
            pipe_path="",
            dispatcher_thread_id="",
            mcp_runtime="",
            mcp_resource="",
        )
        with self.assertRaisesRegex(
            RunnerError, "^Desktop route context is unavailable$"
        ):
            client.register_desktop_route(incomplete)
        with mock.patch.object(
            self.state.desktop_archive_broker,
            "register",
            return_value={"ok": True, "route": "registered"},
        ) as register:
            complete = CodexDesktopRouteContext(
                pipe_path="/tmp/arch-desktop.pipe",
                dispatcher_thread_id="thread-1",
                mcp_runtime="codex",
                mcp_resource="desktop",
            )
            response = client.register_desktop_route(complete)
        self.assertTrue(register.called)
        self.assertIs(response["ok"], True)
        self.assertEqual(register.call_args.args[0].dispatcher_thread_id, "thread-1")

    def test_health_exposes_the_desktop_archive_route(self) -> None:
        client = self.start_service()
        health = client.health()
        self.assertEqual(
            health["desktop_archive_route"],
            self.state.desktop_archive_broker.public_status(),
        )

    def test_desktop_archive_acknowledgement_is_reachable_over_ipc(self) -> None:
        from agent_bridge_connect.service import TaskService

        client = self.start_service()
        board = self.root / "archive-board"
        workspace = self.root / "archive-workspace"
        workspace.mkdir()
        task = TaskService(board, config={"workspace_root": str(self.root)}).create_task(
            "Archive acknowledgement",
            "codex",
            [{"id": 1, "description": "run"}],
            customer_dir=True,
            customer_path=workspace,
        )
        with self.assertRaisesRegex(
            RunnerError, "^Desktop archive acknowledgement requires task and session ids$"
        ):
            client.acknowledge_desktop_archive("", "", board)
        with self.assertRaisesRegex(
            RunnerError, "^Desktop archive acknowledgement has no "
        ):
            client.acknowledge_desktop_archive(task.id, "session-1", board)

    def test_private_channel_round_trip_keeps_channel_directories_clean(self) -> None:
        client = self.start_service()
        channel = "runner-worker-arch-1"
        (self.spool / "requests" / channel).mkdir()
        (self.spool / "responses" / channel).mkdir()
        with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_CHANNEL": channel}):
            channelled = RunnerClient(self.spool, self.token, timeout_s=5.0)
            health = channelled.health()
        self.assertEqual(health["status"], "ready")
        self.assertEqual(list((self.spool / "requests").glob("*.json")), [])
        self.assertEqual(list((self.spool / "responses").glob("*.json")), [])
        self.assertEqual(client.health()["status"], "ready")


class RunnerIpcServiceLifecycleTests(RunnerIpcHarness):
    """Registration state must survive a service restart on the same spool."""

    def test_restart_on_the_same_spool_reacquires_the_endpoint(self) -> None:
        client = self.start_service()
        first_token = self.token.read_text(encoding="utf-8").strip()
        pid_path = self.spool / "runner.pid"
        self.assertEqual(pid_path.read_text(encoding="utf-8").strip(), str(os.getpid()))

        self._stop_service()
        self.assertFalse(pid_path.exists(), "shutdown must release the singleton pid")

        self.state = self._new_state()
        restarted = self.start_service()
        self.assertEqual(pid_path.read_text(encoding="utf-8").strip(), str(os.getpid()))
        self.assertEqual(
            self.token.read_text(encoding="utf-8").strip(),
            first_token,
            "restart must reuse the existing token",
        )
        self.assertEqual(restarted.health()["status"], "ready")
        self.assertEqual(client.health()["status"], "ready")

    def test_second_service_for_the_same_spool_is_rejected(self) -> None:
        self.start_service()
        with self.assertRaisesRegex(RunnerError, "runner already running"):
            RunnerService(self.spool, self.token, self.state, interval_s=0.01)

    def test_service_creates_the_spool_with_owner_only_permissions(self) -> None:
        self.start_service()
        for name in ("requests", "responses", "processing"):
            with self.subTest(directory=name):
                mode = stat.S_IMODE((self.spool / name).stat().st_mode)
                self.assertEqual(mode, 0o700)

    def test_failed_identity_refresh_releases_the_singleton_pid(self) -> None:
        with mock.patch.object(
            RunnerService, "_refresh_identity_files", return_value=False
        ):
            with self.assertRaisesRegex(
                RunnerError, "^runner identity files could not be refreshed$"
            ):
                RunnerService(self.spool, self.token, self.state, interval_s=0.01)
        self.assertFalse((self.spool / "runner.pid").exists())


if __name__ == "__main__":
    unittest.main()
