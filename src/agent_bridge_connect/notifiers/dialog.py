from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.reports import redact_secrets


_BUTTON_RE = re.compile(r"button returned:([^,\n]+)")
_GAVE_UP_RE = re.compile(r"gave up:(true|false)", re.IGNORECASE)
_TEXT_RE = re.compile(r"(?:^|\n)text returned:(.*)\Z", re.DOTALL)
_INPUT_EVENT = "task.input_required"


class DialogNotifier:
    def __init__(self, timeout_s: int = 30, input_timeout_s: int = 300):
        self.timeout_s = max(timeout_s, 1)
        self.input_timeout_s = max(input_timeout_s, 1)
        self.osascript = Path("/usr/bin/osascript")
        self.open_command = Path("/usr/bin/open")

    def send(self, notification: dict) -> DeliveryResult:
        """Show a redacted macOS dialog and return its delivery result."""
        clean = redact_secrets(notification)
        event_type = str(clean.get("event_type", "notification"))
        title = "Agent-Bridge-Connect"
        body = str(clean.get("message", ""))
        report_path = str(clean.get("report_path") or "").strip()
        input_type = str(clean.get("input_type") or "message").strip().lower()
        input_kind = str(clean.get("input_kind") or "").strip().lower()
        response_protocol = str(clean.get("response_protocol") or "").strip().lower()
        input_options = tuple(
            str(option).strip()
            for option in clean.get("input_options", [])
            if str(option).strip()
        ) if isinstance(clean.get("input_options"), list) else ()
        if input_type == "choice" and len(input_options) != 2:
            input_type = "message"
            input_options = ()
        dialog_timeout_s = self.input_timeout_s if event_type == _INPUT_EVENT else self.timeout_s
        script = self._dialog_script(event_type, input_type, dialog_timeout_s)
        try:
            result = subprocess.run(
                [str(self.osascript), "-", title, body, *input_options],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=dialog_timeout_s + 5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DeliveryResult(False, f"dialog notification failed: {exc}")
        if result.returncode != 0:
            if event_type == _INPUT_EVENT and (
                "User canceled" in result.stderr or "(-128)" in result.stderr
            ):
                return DeliveryResult(
                    True,
                    "input dialog dismissed; task remains waiting",
                    f"dialog:{event_type}",
                    {"action": "dismissed"},
                )
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
        if event_type == _INPUT_EVENT:
            action = self._input_action(
                button,
                input_type,
                gave_up,
                input_options,
                input_kind=input_kind,
                response_protocol=response_protocol,
            )
            details = {"action": action}
            if action == "message":
                if input_type == "choice":
                    response = button if button in input_options else ""
                else:
                    text_match = _TEXT_RE.search(stdout)
                    response = text_match.group(1).strip() if text_match else ""
                if not response:
                    return DeliveryResult(
                        True,
                        f"{message}; empty response ignored",
                        f"dialog:{event_type}",
                        {"action": "dismissed"},
                    )
                details["message"] = response
            return DeliveryResult(True, message, f"dialog:{event_type}", details)
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

    def _dialog_script(self, event_type: str, input_type: str, timeout_s: int) -> str:
        if event_type == _INPUT_EVENT and input_type == "permission":
            dialog = (
                'buttons {"Later", "Deny", "Approve"} default button "Later" '
                f'giving up after {timeout_s} with icon caution'
            )
            return (
                "on run argv\n"
                "  set dialogResult to display dialog (item 2 of argv) "
                f"with title (item 1 of argv) {dialog}\n"
                '  return "button returned:" & (button returned of dialogResult) & linefeed & '
                '"gave up:" & ((gave up of dialogResult) as text)\n'
                "end run\n"
            )
        elif event_type == _INPUT_EVENT and input_type == "choice":
            dialog = (
                'buttons {"Later", (item 3 of argv), (item 4 of argv)} default button "Later" '
                f'giving up after {timeout_s} with icon caution'
            )
            return (
                "on run argv\n"
                "  set dialogResult to display dialog (item 2 of argv) "
                f"with title (item 1 of argv) {dialog}\n"
                '  return "button returned:" & (button returned of dialogResult) & linefeed & '
                '"gave up:" & ((gave up of dialogResult) as text)\n'
                "end run\n"
            )
        elif event_type == _INPUT_EVENT:
            dialog = (
                'default answer "" buttons {"Later", "Submit"} default button "Submit" '
                f'giving up after {timeout_s} with icon caution'
            )
            return (
                "on run argv\n"
                "  set dialogResult to display dialog (item 2 of argv) "
                f"with title (item 1 of argv) {dialog}\n"
                '  return "button returned:" & (button returned of dialogResult) & linefeed & '
                '"gave up:" & ((gave up of dialogResult) as text) & linefeed & '
                '"text returned:" & (text returned of dialogResult)\n'
                "end run\n"
            )
        else:
            dialog = (
                'buttons {"OK", "Open Report"} default button "Open Report" '
                f'giving up after {timeout_s} with icon note'
            )
        return (
            "on run argv\n"
            "  display dialog (item 2 of argv) with title (item 1 of argv) "
            f"{dialog}\n"
            "end run\n"
        )

    @staticmethod
    def _input_action(
        button: str,
        input_type: str,
        gave_up: bool,
        input_options: tuple[str, ...] = (),
        input_kind: str = "",
        response_protocol: str = "",
    ) -> str:
        if gave_up or button in {"Later", "unknown"}:
            return "dismissed"
        if input_type == "permission":
            return "approve" if button == "Approve" else "deny" if button == "Deny" else "dismissed"
        if input_type == "choice":
            if input_kind == "resource_limit" and response_protocol == "approve_deny":
                if len(input_options) >= 2:
                    if button == input_options[0]:
                        return "approve"
                    if button == input_options[1]:
                        return "deny"
                return "dismissed"
            return "message" if button in input_options else "dismissed"
        return "message" if button == "Submit" else "dismissed"
