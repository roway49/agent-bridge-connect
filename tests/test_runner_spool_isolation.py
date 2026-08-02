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

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
        return False, None
    try:
        token = (spool / "token").read_text(encoding="utf-8")
    except OSError:
        token = None
    return True, token


def assert_live_spool_untouched(
    test_case: unittest.TestCase, before: tuple[bool, str | None]
) -> None:
    after = snapshot_live_spool()
    test_case.assertEqual(
        after,
        before,
        "uninstall touched the live /tmp/agentbc-runner-v2-UID spool",
    )


def isolated_env(temp_root: Path, *, skip_runner: bool) -> dict[str, str]:
    """Build an environment that points every AgentBC path into the sandbox."""
    env = dict(os.environ)
    env["HOME"] = str(temp_root)
    env["TMPDIR"] = str(temp_root / "tmp")
    env["AGENTBC_RUNNER_SPOOL"] = str(temp_root / "spool")
    env["AGENTBC_CONFIG_PATH"] = str(temp_root / "config.toml")
    env["AGENTBC_ALPHA_HOME"] = str(temp_root / "alpha")
    env["AGENTBC_BIN_DIR"] = str(temp_root / "bin")
    env["HERMES_HOME"] = str(temp_root / "hermes")
    env["AGENTBC_HERMES_SKILL_PATH"] = str(
        temp_root / "hermes" / "skills" / "agentbc" / "SKILL.md"
    )
    env["AGENTBC_CLAUDE_SKILL_PATH"] = str(
        temp_root / "claude" / "skills" / "agentbc" / "SKILL.md"
    )
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
    return (
        (spool / "token").read_text(encoding="utf-8"),
        (spool / "runner.pid").read_text(encoding="utf-8"),
    )


class RunnerSpoolDefaultsTest(unittest.TestCase):
    def test_default_runner_spool_honors_override(self) -> None:
        from agent_bridge_connect.runner import (
            default_runner_spool,
            default_runner_token,
        )

        self.assertEqual(
            default_runner_spool(),
            Path("/tmp") / f"agentbc-runner-v2-{os.getuid()}",
        )
        with tempfile.TemporaryDirectory() as td:
            spool = Path(td) / "spool"
            with mock.patch.dict(os.environ, {"AGENTBC_RUNNER_SPOOL": str(spool)}):
                self.assertEqual(default_runner_spool(), spool)
                self.assertEqual(default_runner_token(), spool / "token")


class PythonUninstallSpoolIsolationTest(unittest.TestCase):
    def _run_uninstall(self, temp_root: Path, *, skip_runner: bool) -> dict:
        from agent_bridge_connect.setup import run_uninstall

        env = isolated_env(temp_root, skip_runner=skip_runner)
        with mock.patch.dict(os.environ, env, clear=False):
            return run_uninstall(
                interactive=False, remove_records=False, remove_artifacts=False
            )

    def test_skip_runner_uninstall_preserves_test_spool_sentinels(self) -> None:
        from agent_bridge_connect.setup import run_uninstall

        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            env = isolated_env(temp, skip_runner=True)
            with mock.patch.dict(os.environ, env, clear=False):
                result = run_uninstall(
                    interactive=False, remove_records=False, remove_artifacts=False
                )

            self.assertTrue(result["ok"], f"uninstall failed: {result}")
            # The skip flag must keep the whole live runtime: spool, token, pid.
            self.assertTrue(
                spool.is_dir(), "skip-runner uninstall deleted the Runner spool"
            )
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
            self.assertFalse(
                spool.exists(), "normal uninstall kept the owned test spool"
            )
            self.assertIn(str(spool), result["removed"])
            self.assertNotIn(str(live_spool_path()), result["removed"])
        assert_live_spool_untouched(self, live_before)


class RunnerStopIsolationTest(unittest.TestCase):
    def test_stop_runner_background_with_isolated_spool_never_touches_live_runner(
        self,
    ) -> None:
        from agent_bridge_connect.runner import stop_runner_background

        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            state = temp / "state"
            make_test_spool(spool)
            state.mkdir(parents=True)
            # Sentinel pid that does not exist as a process; must be treated as stale.
            (spool / "runner.pid").write_text(PID_SENTINEL, encoding="utf-8")
            env = isolated_env(temp, skip_runner=False)
            with mock.patch.dict(os.environ, env, clear=False):
                result = stop_runner_background(
                    spool_root=spool, state_root=state, timeout_s=1.0
                )
            self.assertTrue(result["ok"], f"runner stop failed: {result}")
            self.assertEqual(result["pids"], [])
            self.assertEqual(result["status"], "not_running")
        assert_live_spool_untouched(self, live_before)


class FallbackShellUninstallSpoolIsolationTest(unittest.TestCase):
    def _run_fallback(
        self, temp_root: Path, *, skip_runner: bool
    ) -> subprocess.CompletedProcess[str]:
        env = isolated_env(temp_root, skip_runner=skip_runner)
        return subprocess.run(
            ["bash", str(FALLBACK_SCRIPT), "--keep-records", "--keep-artifacts"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_fallback_skip_runner_uninstall_preserves_test_spool_sentinels(
        self,
    ) -> None:
        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            result = self._run_fallback(temp, skip_runner=True)

            self.assertEqual(
                result.returncode,
                0,
                f"fallback uninstall failed:\n{result.stdout}\n{result.stderr}",
            )
            self.assertTrue(
                spool.is_dir(),
                "fallback skip-runner uninstall deleted the Runner spool",
            )
            self.assertEqual(read_sentinel(spool), (TOKEN_SENTINEL, PID_SENTINEL))
        assert_live_spool_untouched(self, live_before)

    def test_fallback_isolated_normal_uninstall_removes_only_owned_test_spool(
        self,
    ) -> None:
        # A real AgentBC-owned launch agent must never be removed by this test;
        # the sandboxed HOME guarantees the plist lookup stays inside the temp root.
        live_before = snapshot_live_spool()
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            spool = temp / "spool"
            make_test_spool(spool)
            result = self._run_fallback(temp, skip_runner=False)

            self.assertEqual(
                result.returncode,
                0,
                f"fallback uninstall failed:\n{result.stdout}\n{result.stderr}",
            )
            self.assertFalse(
                spool.exists(), "fallback normal uninstall kept the owned test spool"
            )
        assert_live_spool_untouched(self, live_before)


if __name__ == "__main__":
    unittest.main()
