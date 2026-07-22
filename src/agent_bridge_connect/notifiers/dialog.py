from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.reports import redact_secrets


_BUTTON_RE = re.compile(r"button returned:([^,\n]+)")
_GAVE_UP_RE = re.compile(r"gave up:(true|false)", re.IGNORECASE)


class DialogNotifier:
    def __init__(self, timeout_s: int = 30):
        self.timeout_s = max(timeout_s, 1)
        self.osascript = Path("/usr/bin/osascript")
        self.open_command = Path("/usr/bin/open")

    def send(self, notification: dict) -> DeliveryResult:
        """Show a redacted macOS dialog and return its delivery result."""
        clean = redact_secrets(notification)
        event_type = str(clean.get("event_type", "notification"))
        title = "Agent-Bridge-Connect"
        body = str(clean.get("message", ""))
        report_path = str(clean.get("report_path") or "").strip()
        script = (
            "on run argv\n"
            "  display dialog (item 2 of argv) with title (item 1 of argv) "
            f'buttons {{"OK", "Open Report"}} default button "Open Report" '
            f'giving up after {self.timeout_s} with icon note\n'
            "end run\n"
        )
        try:
            result = subprocess.run(
                [str(self.osascript), "-", title, body],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_s + 5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DeliveryResult(False, f"dialog notification failed: {exc}")
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
            return DeliveryResult(
                False,
                f"dialog notification failed; osascript exited {result.returncode}: {detail}",
                f"dialog:{event_type}",
            )

        stdout = result.stdout or ""
        button_match = _BUTTON_RE.search(stdout)
        button = button_match.group(1).strip() if button_match else "unknown"
        gave_up_match = _GAVE_UP_RE.search(stdout)
        gave_up = gave_up_match.group(1).lower() == "true" if gave_up_match else False
        message = f"dialog shown; button={button}; gave_up={str(gave_up).lower()}"
        if button == "Open Report" and report_path:
            try:
                subprocess.run(
                    [str(self.open_command), report_path],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DeliveryResult(
                    False,
                    f"{message}; open report failed: {exc}",
                    f"dialog:{event_type}",
                )
            message = f"{message}; opened report={report_path}"
        return DeliveryResult(True, message, f"dialog:{event_type}")
