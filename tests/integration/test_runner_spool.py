"""Regression coverage for Runner spool isolation from uninstall paths.

The live Runner spool lives at ``/tmp/agentbc-runner-v2-<uid>`` and contains the
Runner token, pid file, and request/response queues. Uninstall tests must never
touch that directory. Every test in this module runs with ``AGENTBC_RUNNER_SPOOL``
pointed at a task-scoped temporary directory and a temporary ``HOME`` so all
uninstall targets (config, skills, alpha install root, launch-agent plist) are
contained inside the temporary sandbox.

Contracts under test:

* ``AGENTBC_RUNNER_SPOOL`` overrides ``default_runner_spool()`` everywhere.
* ``AGENTBC_UNINSTALL_SKIP_RUNNER=1`` preserves the entire live Runner spool
  (token and pid sentinels stay intact) during Python uninstall.
* An explicitly isolated normal uninstall removes only its owned test spool and
  leaves the live ``/tmp/agentbc-runner-v2-<uid>`` directory untouched.
* The fallback shell uninstaller honors the same two environment-variable
  contracts.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import plistlib
import shutil
import socket
from unittest.mock import patch
from urllib.request import Request, urlopen

from agent_bridge_connect.config import write_config_atomic
from agent_bridge_connect.protocol import ABCError
from agent_bridge_connect.web_service import Service, allocate_port

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import os as _run_lease_race_os
import tempfile as _run_lease_race_tempfile
import unittest as _run_lease_race_unittest
from datetime import datetime as _run_lease_race_datetime, timedelta as _run_lease_race_timedelta, timezone as _run_lease_race_timezone
from pathlib import Path as _run_lease_race_Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
FALLBACK_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall_fallback.sh"
TOKEN_SENTINEL = "uninstall-skip-sentinel-token\n"
PID_SENTINEL = "424242\n"


def live_spool_path() -> Path:
    return Path("/tmp") / f"agentbc-runner-v2-{os.getuid()}"


def snapshot_live_spool() -> tuple[bool, str | None]:
    """Record whether the live spool exists and its token content, if any."""
    spool = live_spool_path()
    if not spool.exists():
        return (False, None)
    try:
        token = (spool / "token").read_text(encoding="utf-8")
    except OSError:
        token = None
    return (True, token)


def assert_live_spool_untouched(test_case: unittest.TestCase, before: tuple[bool, str | None]) -> None:
    after = snapshot_live_spool()
    test_case.assertEqual(after, before, "uninstall touched the live /tmp/agentbc-runner-v2-UID spool")


def isolated_env(temp_root: Path, *, skip_runner: bool) -> dict[str, str]:
    """Build an environment that points every AgentBC path into the sandbox."""
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = str(temp_root)
    env["TMPDIR"] = str(temp_root / "tmp")
    env["AGENTBC_RUNNER_SPOOL"] = str(temp_root / "spool")
    env["AGENTBC_CONFIG_PATH"] = str(temp_root / "config.toml")
    env["AGENTBC_ALPHA_HOME"] = str(temp_root / "alpha")
    env["AGENTBC_BIN_DIR"] = str(temp_root / "bin")
    env["HERMES_HOME"] = str(temp_root / "hermes")
    env["AGENTBC_HERMES_SKILL_PATH"] = str(temp_root / "hermes" / "skills" / "agentbc" / "SKILL.md")
    env["AGENTBC_CLAUDE_SKILL_PATH"] = str(temp_root / "claude" / "skills" / "agentbc" / "SKILL.md")
    env["AGENTBC_CODEX_SKILL_PATH"] = str(temp_root / "codex" / "skills" / "agentbc")
    if skip_runner:
        env["AGENTBC_UNINSTALL_SKIP_RUNNER"] = "1"
    else:
        env.pop("AGENTBC_UNINSTALL_SKIP_RUNNER", None)
    return env


def make_test_spool(spool: Path) -> None:
    spool.mkdir(parents=True)
    (spool / "token").write_text(TOKEN_SENTINEL, encoding="utf-8")
    (spool / "runner.pid").write_text(PID_SENTINEL, encoding="utf-8")
    (spool / "requests").mkdir()
    (spool / "responses").mkdir()
    (spool / "processing").mkdir()


def read_sentinel(spool: Path) -> tuple[str, str]:
    return ((spool / "token").read_text(encoding="utf-8"), (spool / "runner.pid").read_text(encoding="utf-8"))


class RunnerSpoolDefaultsTest(unittest.TestCase):
    def test_default_runner_spool_honors_override(self) -> None:
        from agent_bridge_connect.runner import default_runner_spool, default_runner_token

        self.assertEqual(default_runner_spool(), Path("/tmp") / f"agentbc-runner-v2-{os.getuid()}")
        with tempfile.TemporaryDirectory() as td:
            spool = Path(td) / "spool"
            with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_SPOOL": str(spool)}):
                self.assertEqual(default_runner_spool(), spool)
                self.assertEqual(default_runner_token(), spool / "token")


class PythonUninstallSpoolIsolationTest(unittest.TestCase):
    def setUp(self):
        shared = mock.patch("agent_bridge_connect.installation.shared_entries", return_value=())
        shared.start()
        self.addCleanup(shared.stop)

    def _run_uninstall(self, temp_root: Path, *, skip_runner: bool) -> dict:
        from agent_bridge_connect.setup import run_uninstall

        env = isolated_env(temp_root, skip_runner=skip_runner)
        with mock.patch.dict(os.environ, env, clear=False):
            return run_uninstall(interactive=False, remove_records=False, remove_artifacts=False)

    def test_skip_runner_uninstall_preserves_test_spool_sentinels(self) -> None:
        from agent_bridge_connect.setup import run_uninstall

        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            env = isolated_env(temp, skip_runner=True)
            with mock.patch.dict(os.environ, env, clear=False):
                result = run_uninstall(interactive=False, remove_records=False, remove_artifacts=False)
            self.assertTrue(result["ok"], f"uninstall failed: {result}")
            self.assertTrue(spool.is_dir(), "skip-runner uninstall deleted the Runner spool")
            self.assertEqual(read_sentinel(spool), (TOKEN_SENTINEL, PID_SENTINEL))
            self.assertNotIn(str(spool), result["removed"])
            self.assertNotIn(str(live_spool_path()), result["removed"])
        assert_live_spool_untouched(self, live_before)

    def test_isolated_normal_uninstall_removes_only_owned_test_spool(self) -> None:
        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            result = self._run_uninstall(temp, skip_runner=False)
            self.assertTrue(result["ok"], f"uninstall failed: {result}")
            self.assertFalse(spool.exists(), "normal uninstall kept the owned test spool")
            self.assertIn(str(spool), result["removed"])
            self.assertNotIn(str(live_spool_path()), result["removed"])
        assert_live_spool_untouched(self, live_before)


class RunnerStopIsolationTest(unittest.TestCase):
    def test_env_selected_spool_does_not_stop_shared_state_pid(self) -> None:
        from agent_bridge_connect.runner import RunnerClient, stop_runner_background

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shared = root / "shared-state"
            spool = root / "isolated-spool"
            shared.mkdir()
            spool.mkdir()
            (shared / "runner.pid").write_text("10101\n")
            (spool / "runner.pid").write_text("20202\n")
            with (
                mock.patch.dict(os.environ, {"AGENTBC_RUNNER_SPOOL": str(spool)}),
                mock.patch("agent_bridge_connect.runner.default_runner_root", return_value=shared),
                mock.patch("agent_bridge_connect.runner._pid_is_alive", return_value=True),
                mock.patch("agent_bridge_connect.runner._discover_runner_pids", return_value=[20202]),
                mock.patch("agent_bridge_connect.runner._pid_matches_runner_spool", side_effect=lambda pid, root: pid == 20202),
                mock.patch("agent_bridge_connect.runner._pid_matches_runner_state", return_value=True),
                mock.patch.object(RunnerClient, "health", return_value={"ok": True, "status": "ready", "pid": 20202}),
                mock.patch("agent_bridge_connect.runner._terminate_runner_pids", return_value=([20202], [], [])) as terminate,
                mock.patch("agent_bridge_connect.runner.cleanup_legacy_runner_launch_agent") as legacy,
            ):
                result = stop_runner_background(spool_root=spool)
            self.assertEqual(result["pids"], [20202])
            self.assertEqual(terminate.call_args.args[0], {20202})
            legacy.assert_not_called()
            self.assertEqual((shared / "runner.pid").read_text(), "10101\n")
            self.assertFalse((spool / "runner.pid").exists())

    def test_stop_runner_background_with_isolated_spool_never_touches_live_runner(self) -> None:
        from agent_bridge_connect.runner import stop_runner_background

        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            state = temp / "state"
            make_test_spool(spool)
            state.mkdir(parents=True)
            (spool / "runner.pid").write_text(PID_SENTINEL, encoding="utf-8")
            env = isolated_env(temp, skip_runner=False)
            with mock.patch.dict(os.environ, env, clear=False):
                result = stop_runner_background(spool_root=spool, state_root=state, timeout_s=1.0)
            self.assertTrue(result["ok"], f"runner stop failed: {result}")
            self.assertEqual(result["pids"], [])
            self.assertEqual(result["status"], "not_running")
        assert_live_spool_untouched(self, live_before)


class FallbackShellUninstallSpoolIsolationTest(unittest.TestCase):
    def _run_fallback(self, temp_root: Path, *, skip_runner: bool) -> subprocess.CompletedProcess[str]:
        env = isolated_env(temp_root, skip_runner=skip_runner)
        return subprocess.run(["bash", str(FALLBACK_SCRIPT), "--keep-records", "--keep-artifacts"], cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=60, check=False)

    def test_fallback_skip_runner_uninstall_preserves_test_spool_sentinels(self) -> None:
        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            result = self._run_fallback(temp, skip_runner=True)
            self.assertEqual(result.returncode, 0, f"fallback uninstall failed:\n{result.stdout}\n{result.stderr}")
            self.assertTrue(spool.is_dir(), "fallback skip-runner uninstall deleted the Runner spool")
            self.assertEqual(read_sentinel(spool), (TOKEN_SENTINEL, PID_SENTINEL))
        assert_live_spool_untouched(self, live_before)

    def test_fallback_isolated_normal_uninstall_removes_only_owned_test_spool(self) -> None:
        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            result = self._run_fallback(temp, skip_runner=False)
            self.assertEqual(result.returncode, 0, f"fallback uninstall failed:\n{result.stdout}\n{result.stderr}")
            self.assertFalse(spool.exists(), "fallback normal uninstall kept the owned test spool")
        assert_live_spool_untouched(self, live_before)


class _run_lease_race_RunLeaseFinalizeRaceTests(_run_lease_race_unittest.TestCase):
    def setUp(self) -> None:
        from agent_bridge_connect.service import TaskService

        self.temporary = _run_lease_race_tempfile.TemporaryDirectory()
        self.board = _run_lease_race_Path(self.temporary.name) / "record"
        self.service = TaskService(self.board, config={"workspace_root": str(_run_lease_race_Path(self.temporary.name) / "workspace")})
        self.task = self.service.create_task("Worker finalization race", "shell", [{"id": 1, "description": "finish normally"}], customer_dir=False)
        task = self.service.store.read_task(self.task.id)
        task["status"] = "working"
        task.setdefault("extensions", {}).setdefault("agentbc.execution", {})["worker_pid"] = _run_lease_race_os.getpid()
        self.service.store.write_task(self.task.id, task)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _lost_executor_lease(self, *, age_s: int = 0) -> None:
        from agent_bridge_connect.run_lease import create_lease, save_lease

        lease = create_lease(self.task.id, "shell", 99999999, str(self.board))
        lease.last_heartbeat_at = (_run_lease_race_datetime.now(_run_lease_race_timezone.utc) - _run_lease_race_timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")
        save_lease(lease, self.board)

    def test_live_worker_gets_finalize_grace_after_executor_exit(self) -> None:
        from agent_bridge_connect.run_lease import reconcile_task

        self._lost_executor_lease()
        self.assertEqual(reconcile_task(self.task.id, self.board), "active")
        current = self.service.store.read_task(self.task.id)
        self.assertEqual(current["status"], "working")
        self.assertFalse(any((event.get("event_type") == "task.failed" for event in self.service.store.read_events(self.task.id))))

    def test_live_worker_becomes_stale_not_failed_after_grace(self) -> None:
        from agent_bridge_connect.run_lease import reconcile_task

        self._lost_executor_lease(age_s=60)
        self.assertEqual(reconcile_task(self.task.id, self.board), "stale")
        current = self.service.store.read_task(self.task.id)
        self.assertEqual(current["status"], "working")


# Independent WebUI lifecycle shares the owned-service isolation contract.
class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="webui 中文 ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.toml"
        self.board = self.root / "record"
        (self.board / "ABCD/001").mkdir(parents=True)
        report = self.root / "tasks/report"
        report.mkdir(parents=True)
        (report / "result.md").write_text("中文 report")
        (self.board / "ABCD/001/task.json").write_text(
            json.dumps(
                {
                    "id": "ABCD-001",
                    "title": "中文  task",
                    "status": "completed",
                    "steps": [],
                    "extensions": {},
                }
            )
        )
        self.port = allocate_port()
        write_config_atomic(
            {
                "workspace_root": str(self.root),
                "board_root": str(self.board),
                "webui": {
                    "preferred_view": "webui",
                    "port": self.port,
                    "autostart": False,
                },
            },
            self.config,
        )
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<!doctype html><title>fixture</title>")
        self.service = Service(self.config, assets=assets)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.service.close()
        shutil.rmtree(self.service.root)

    def fingerprints(self):
        return {
            str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_concurrent_start_private_receipt_and_readonly_close(self):
        before = self.fingerprints()
        with ThreadPoolExecutor(max_workers=4) as pool:
            urls = list(
                pool.map(lambda _: self.service.start(open_browser=False), range(4))
            )
        self.assertEqual(len(set(urls)), 1)
        receipt = self.service.read()
        self.assertEqual(self.service.record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.service.root.stat().st_mode & 0o777, 0o700)
        req = Request(
            urls[0].split("/#")[0] + "/api/v1/tasks?group=completed",
            headers={"Authorization": "Bearer " + receipt["token"]},
        )
        with urlopen(req, timeout=3) as response:
            self.assertEqual(json.load(response)["items"][0]["task_id"], "ABCD-001")
        status = self.service.status()
        self.assertTrue(status["running"])
        self.assertNotIn(receipt["token"], json.dumps(status))
        self.assertIsNone(
            self.service.request({**receipt, "secret": "incorrect"}, "close")
        )
        self.assertTrue(self.service.status()["running"])
        self.service.close()
        self.service.close()
        self.assertFalse(self.service.status()["running"])
        self.assertEqual(before, self.fingerprints())
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
        new_url = self.service.start(open_browser=False)
        self.assertNotEqual(new_url, urls[0])

    def test_foreign_port_and_stale_pid_are_never_killed(self):
        self.service.prepare()
        self.service.record.write_text(
            json.dumps(
                {
                    "config": str(self.config.resolve()),
                    "pid": os.getpid(),
                    "instance": "stale",
                    "secret": "stale",
                }
            )
        )
        self.service.record.chmod(0o600)
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", self.port))
            foreign.listen()
            with self.assertRaises(ABCError):
                self.service.start(open_browser=False)
            self.assertEqual(self.service.status()["error"], "webui_port_in_use")
            self.service.close()
            self.assertEqual(foreign.getsockname()[1], self.port)
        self.assertFalse(self.service.status()["running"])

    def test_update_restores_running_service_without_browser(self):
        self.service.start(open_browser=False)
        old = self.service.read()["instance"]
        state = self.service.suspend_update()
        self.assertFalse(self.service.status()["running"])
        self.service.restore_update(state)
        self.assertTrue(self.service.status()["running"])
        self.assertNotEqual(old, self.service.read()["instance"])

    def test_launch_registration_is_user_scoped_and_not_keepalive(self):
        self.service.plist = (
            self.root / "LaunchAgents" / (self.service.label + ".plist")
        )
        with (
            patch("agent_bridge_connect.web_service.sys.platform", "darwin"),
            patch("agent_bridge_connect.web_service.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            self.service.login(True)
            data = plistlib.loads(self.service.plist.read_bytes())
            self.assertTrue(data["RunAtLoad"])
            self.assertFalse(data["KeepAlive"])
            self.assertIn("--launch", data["ProgramArguments"])
            self.assertNotIn("token", str(data))
            self.service.close()
            self.assertTrue(self.service.plist.exists())
            self.service.login(False)
            self.assertFalse(self.service.plist.exists())

    def test_configuration_identity_and_private_state(self):
        self.service.prepare()
        self.service.record.write_text(json.dumps({"config": "another"}))
        self.service.record.chmod(0o600)
        with self.assertRaises(ABCError):
            self.service.status()
        self.service.record.unlink()
        self.service.root.chmod(0o755)
        try:
            with self.assertRaises(ABCError):
                self.service.start(open_browser=False)
        finally:
            self.service.root.chmod(0o700)


class ContainerOriginTests(unittest.TestCase):
    def test_explicit_origin_required_and_enforced(self):
        from agent_bridge_connect.web_gateway import WebServer
        from agent_bridge_connect.web_snapshot import SnapshotProvider
        from http.client import HTTPConnection
        import threading

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "record").mkdir()
            (root / "index.html").write_text("<html>fixture</html>")
            provider = SnapshotProvider(root / "record")
            with self.assertRaises(ABCError):
                WebServer(provider, root, listen_host="0.0.0.0")
            for origin in ("http://example.com:1234", "http://127.0.0.1:1234/", "*"):
                with self.assertRaises(ABCError):
                    WebServer(
                        provider, root, listen_host="0.0.0.0", public_origin=origin
                    )
            with WebServer(
                provider,
                root,
                listen_host="0.0.0.0",
                public_origin="http://127.0.0.1:12345",
            ) as server:
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.01},
                    daemon=True,
                )
                thread.start()
                try:
                    for host, expected in [
                        ("127.0.0.1:12345", 200),
                        ("127.0.0.1:54321", 403),
                    ]:
                        conn = HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=2
                        )
                        conn.request("GET", "/", headers={"Host": host})
                        response = conn.getresponse()
                        self.assertEqual(response.status, expected)
                        response.read()
                        conn.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=2)
