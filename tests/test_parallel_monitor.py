from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


def _load_monitor_module():
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "archive_support"
        / "scripts"
        / "agentbc_parallel_monitor.py"
    )
    spec = importlib.util.spec_from_file_location("agentbc_parallel_monitor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load agentbc_parallel_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParallelMonitorTests(unittest.TestCase):
    def test_process_group_summary_splits_gui_and_workers(self) -> None:
        monitor = _load_monitor_module()
        rows = [
            {
                "command": "python -m agent_bridge_connect.cli worker run --executor codex",
                "pcpu": 10.0,
                "rss_kb": 100_000,
                "group": monitor.classify_process(
                    "python -m agent_bridge_connect.cli worker run --executor codex"
                ),
            },
            {
                "command": "/Applications/Codex.app/Contents/MacOS/Codex",
                "pcpu": 2.0,
                "rss_kb": 500_000,
                "group": monitor.classify_process("/Applications/Codex.app/Contents/MacOS/Codex"),
            },
            {
                "command": "/Users/tester/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main dashboard",
                "pcpu": 1.0,
                "rss_kb": 900_000,
                "group": monitor.classify_process(
                    "/Users/tester/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main dashboard"
                ),
            },
        ]

        groups = monitor.summarize_process_groups(rows)

        self.assertEqual(groups["agentbc_worker"]["rss_mb_sum"], 97.7)
        self.assertEqual(groups["codex_gui"]["rss_mb_sum"], 488.3)
        self.assertEqual(groups["hermes_gui"]["rss_mb_sum"], 878.9)

    def test_sample_processes_prefers_runner_process_sample(self) -> None:
        monitor = _load_monitor_module()
        runner_payload = {
            "ok": True,
            "source": "runner_ps",
            "count": 1,
            "cpu_sum": 3.0,
            "rss_kb_sum": 2048,
            "rss_mb_sum": 2.0,
            "groups": {"agentbc_runner": {"count": 1, "cpu_sum": 3.0, "rss_kb_sum": 2048, "rss_mb_sum": 2.0}},
            "top": [],
            "ps_ok": True,
            "ps_error": "",
        }

        with mock.patch.object(
            monitor,
            "run_command",
            return_value={
                "ok": True,
                "returncode": 0,
                "stdout": json.dumps(runner_payload),
                "stderr": "",
                "elapsed_s": 0.01,
            },
        ) as run:
            sample = monitor.sample_processes(("agentbc",))

        run.assert_called_once_with(
            ["agentbc", "runner", "process-sample", "--pattern", "agentbc"],
            timeout=7.0,
        )
        self.assertEqual(sample["source"], "runner_ps")
        self.assertEqual(sample["rss_mb_sum"], 2.0)


if __name__ == "__main__":
    unittest.main()
