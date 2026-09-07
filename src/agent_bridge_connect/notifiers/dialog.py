from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

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
        # The compact bounded ``AgentBC · <Executor> · <Task ID>`` title travels
        # only on the explicit ``dialog_title`` field so the generic payload
        # ``title`` (if any) keeps being ignored exactly as before.
        title = str(clean.get("dialog_title") or "Agent-Bridge-Connect")
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
        if event_type == _INPUT_EVENT and input_type == "permission":
            if clean.get("native_live_elevation") is True:
                return self._send_live_claude_elevation_dialog(
                    clean, title, dialog_timeout_s
                )
            if int(clean.get("approval_version") or 1) == 3 and str(
                clean.get("elevation_mode") or ""
            ).strip().lower() in {"full", "contained_full"}:
                return self._send_task_elevation_dialog(clean, title, dialog_timeout_s)
            return self._send_permission_dialog(clean, title, dialog_timeout_s)
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
        except subprocess.TimeoutExpired:
            return DeliveryResult(False, "dialog notification timed out")
        except OSError as exc:
            return DeliveryResult(False, f"dialog notification failed: {exc}")
        if result.returncode != 0:
            if event_type == _INPUT_EVENT and (
                "User canceled" in result.stderr or "(-128)" in result.stderr
            ):
                if input_type == "permission":
                    return DeliveryResult(
                        True,
                        "permission dialog closed; request denied",
                        f"dialog:{event_type}",
                        {"action": "deny", "decision_source": "dialog_closed"},
                    )
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
            if input_type == "permission":
                details["decision_source"] = (
                    "timeout"
                    if gave_up
                    else "user"
                    if button in {"Approve", "Deny"}
                    else "fail_closed"
                )
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

    def _send_permission_dialog(
        self,
        clean: dict,
        title: str,
        dialog_timeout_s: int,
    ) -> DeliveryResult:
        """Show the minimal Approve/Deny permission dialog with optional detail.

        PERM-104-002 v2: the decision view offers exactly the executor-native
        choices recorded on the pending request (``native_options``), never an
        inferred allow category and never ``full``.  ``Choose Permission``
        opens a numbered native-choice picker; the selected handle is returned
        for the exact request.  ``View Details`` is a non-decision interaction
        opening a bounded read-only detail view whose ``Back`` button returns
        to the decision view without responding.  Closing either view or
        reaching the deadline auto-denies exactly once with an auditable
        ``decision_source``; there is no allow default.
        """
        summary = str(clean.get("reason_summary") or clean.get("message") or "").strip()
        detail = str(clean.get("reason_detail") or summary).strip()
        has_detail = bool(detail)
        deadline_at = str(clean.get("deadline_at") or "")
        native_options = [
            option
            for option in clean.get("native_options", [])
            if isinstance(option, dict) and str(option.get("label") or "").strip()
        ] if isinstance(clean.get("native_options"), list) else []
        # Deterministic identity rendering: prefer the explicit sanitized bounded
        # fields on the payload; fall back safely to the task id / generic facts
        # so payloads carrying only identity context still show a readable view.
        identity_task_id = str(clean.get("identity_task_id") or clean.get("task_id") or "").strip()
        identity_title = str(clean.get("identity_task_title") or "").strip()
        identity_executor = str(clean.get("identity_executor") or "").strip()
        identity_blocked_step = str(clean.get("identity_blocked_step") or "").strip()
        identity_scope = str(clean.get("identity_scope") or "").strip()
        has_identity_context = bool(
            identity_task_id or identity_title or identity_blocked_step or identity_scope
        )
        if has_identity_context:
            if not identity_scope:
                requested = str(clean.get("requested_permission") or "").strip().lower()
                identity_scope = "full" if requested == "full" else "unknown"
            if not identity_executor:
                identity_executor = "unknown"
            body_lines = []
            if identity_task_id:
                body_lines.append(f"Task: {identity_task_id}")
            if identity_title:
                body_lines.append(f"Title: {identity_title}")
            if identity_blocked_step:
                body_lines.append(f"Blocked step: {identity_blocked_step}")
            if identity_scope:
                body_lines.append(f"Permission scope: {identity_scope}")
            body_lines.append(f"Executor: {identity_executor}")
            body_lines.append("")
            body_lines.append(summary)
            decision_body = "\n".join(body_lines)
        else:
            # Keep the minimal legacy body byte-for-byte when the payload does
            # not carry identity facts.
            decision_body = summary
        while True:
            give_up_s = self._permission_give_up_seconds(deadline_at, dialog_timeout_s)
            if give_up_s <= 0:
                # The absolute deadline is already reached or has less than one
                # second left: fail closed before showing (or re-showing) any
                # decision/detail view.
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog timed out; request denied",
                    "timeout",
                )
            decision_script = self._permission_decision_script(
                give_up_s,
                has_detail,
                has_options=bool(native_options),
            )
            decision = self._run_script(
                title,
                decision_body,
                decision_script,
                give_up_s,
            )
            if decision is None:
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog closed; request denied",
                    "dialog_closed",
                )
            if decision == "timed_out":
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog timed out; request denied",
                    "timeout",
                )
            if isinstance(decision, DeliveryResult):
                return decision
            button = str(decision)
            if native_options and button == "Deny":
                return self._native_deny_or_legacy_result(
                    native_options,
                    f"dialog shown; button={button}; gave_up=false",
                    "user",
                )
            if native_options and button == "Approve":
                option_result = self._choose_permission_option(
                    title,
                    native_options,
                    deadline_at,
                    dialog_timeout_s,
                )
                if isinstance(option_result, DeliveryResult):
                    return option_result
                # Back is navigation only: re-open the first-level dialog and
                # do not persist or return any decision.
                continue
            action = self._input_action(button, "permission", False)
            if action == "approve":
                return DeliveryResult(
                    True,
                    f"dialog shown; button={button}; gave_up=false",
                    f"dialog:{_INPUT_EVENT}",
                    {"action": "approve", "decision_source": "user"},
                )
            if action == "deny":
                decision_source = (
                    "user" if button in {"Approve", "Deny"} else "fail_closed"
                )
                return DeliveryResult(
                    True,
                    f"dialog shown; button={button}; gave_up=false",
                    f"dialog:{_INPUT_EVENT}",
                    {"action": "deny", "decision_source": decision_source},
                )
            if not has_detail:
                # Defensive: a View Details button without a bounded detail must
                # not respond; return to the decision view.
                continue
            # Non-decision View Details: show the bounded read-only detail view.
            detail_give_up_s = self._permission_give_up_seconds(deadline_at, dialog_timeout_s)
            if detail_give_up_s <= 0:
                # Reaching the total deadline while the decision view is showing
                # (or before re-showing a detail view) must fail closed before
                # any further dialog is displayed.
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog timed out; request denied",
                    "timeout",
                )
            detail_script = self._permission_detail_script(detail_give_up_s)
            detail_view = self._run_script(
                title,
                detail,
                detail_script,
                detail_give_up_s,
            )
            if detail_view == "timed_out":
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission detail timed out; request denied",
                    "timeout",
                )
            if detail_view is None:
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission detail closed; request denied",
                    "dialog_closed",
                )
            if isinstance(detail_view, DeliveryResult):
                return detail_view
            # Only ``Back`` returns to the decision view without responding.
            if str(detail_view).strip() == "Back":
                continue
            return self._native_deny_or_legacy_result(
                native_options,
                "permission detail closed; request denied",
                "dialog_closed",
            )

    def _choose_permission_option(
        self,
        title: str,
        native_options: list[dict[str, Any]],
        deadline_at: str,
        dialog_timeout_s: int,
    ) -> DeliveryResult | None:
        """Show the native approval-scope picker (one absolute deadline).

        Returns the selected choice DeliveryResult, ``None`` for Back/unknown
        (returning to the decision view), or a denial DeliveryResult on
        close/timeout.  Non-selectable choices are listed as informational
        rows and can never be picked.
        """
        selectable = {
            str(option.get("kind") or ""): option
            for option in native_options
            if option.get("selectable", True) is not False
            and str(option.get("kind") or "") in {"once", "session"}
        }
        if "once" not in selectable and "session" not in selectable:
            return None
        while True:
            give_up_s = self._permission_give_up_seconds(deadline_at, dialog_timeout_s)
            if give_up_s <= 0:
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog timed out; request denied",
                    "timeout",
                )
            body = "Choose how long to allow this action."
            script = self._permission_choice_script(
                give_up_s,
                has_once="once" in selectable,
                has_session="session" in selectable,
            )
            result = self._run_script(title, body, script, give_up_s)
            if result is None:
                return None
            if result == "timed_out":
                return self._native_deny_or_legacy_result(
                    native_options,
                    "permission dialog timed out; request denied",
                    "timeout",
                )
            if isinstance(result, DeliveryResult):
                return result
            button = str(result).strip()
            if button == "Back":
                return None
            selected_kind = "once" if button == "Once" else "session" if button == "This Session" else ""
            if selected_kind in selectable:
                option = selectable[selected_kind]
                return DeliveryResult(
                    True,
                    f"dialog shown; choice={selected_kind}; gave_up=false",
                    f"dialog:{_INPUT_EVENT}",
                    {
                        "action": "permission_option",
                        "decision_source": "user",
                        "option_handle": str(option.get("handle") or ""),
                        "option_kind": str(option.get("kind") or ""),
                    },
                )
            # Any other button is fail-closed: treat as denial once.
            return self._native_deny_or_legacy_result(
                native_options,
                "permission dialog closed; request denied",
                "fail_closed",
            )

    def _permission_choice_script(
        self,
        give_up_s: int,
        *,
        has_once: bool,
        has_session: bool,
    ) -> str:
        """Build the second-level Back / Once / This Session dialog."""
        buttons = ["Back"]
        if has_once:
            buttons.append("Once")
        if has_session:
            buttons.append("This Session")
        button_list = ", ".join(f'"{button}"' for button in buttons)
        dialog = (
            f'buttons {{{button_list}}} default button 1 '
            f"giving up after {give_up_s} with icon caution"
        )
        return (
            "on run argv\n"
            "  set dialogResult to display dialog (item 2 of argv) "
            f"with title (item 1 of argv) {dialog}\n"
            '  return "button returned:" & (button returned of dialogResult) & linefeed & '
            '"gave up:" & ((gave up of dialogResult) as text)\n'
            "end run\n"
        )

    def _run_script(
        self,
        title: str,
        body: str,
        script: str,
        give_up_s: int,
    ) -> str | None | DeliveryResult:
        """Run one osascript dialog; return button, ``None`` on close, or result."""
        try:
            result = subprocess.run(
                [str(self.osascript), "-", title, body],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=give_up_s + 5,
            )
        except subprocess.TimeoutExpired:
            return "timed_out"
        except OSError as exc:
            return DeliveryResult(False, f"dialog notification failed: {exc}")
        if result.returncode != 0:
            if "User canceled" in result.stderr or "(-128)" in result.stderr:
                return None
            detail_msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
            return DeliveryResult(
                False,
                f"dialog notification failed; osascript exited {result.returncode}: {detail_msg}",
                f"dialog:{_INPUT_EVENT}",
            )
        stdout = result.stdout or ""
        button_match = _BUTTON_RE.search(stdout)
        button = button_match.group(1).strip() if button_match else "unknown"
        gave_up_match = _GAVE_UP_RE.search(stdout)
        gave_up = gave_up_match.group(1).lower() == "true" if gave_up_match else False
        if gave_up:
            return "timed_out"
        return button

    def _permission_decision_script(
        self, give_up_s: int, has_detail: bool, *, has_options: bool = False
    ) -> str:
        buttons: list[str] = ["Deny", "Approve"]
        if has_options:
            # First level is navigation plus explicit deny.  Approve opens the
            # native scope picker and carries no receipt by itself.
            buttons = ["View Details", "Deny", "Approve"]
        elif has_detail:
            buttons = ["View Details", *buttons]
        button_list = ", ".join(f'"{button}"' for button in buttons)
        dialog = (
            f'buttons {{{button_list}}} default button "Deny" '
            f"giving up after {give_up_s} with icon caution"
        )
        return (
            "on run argv\n"
            "  set dialogResult to display dialog (item 2 of argv) "
            f"with title (item 1 of argv) {dialog}\n"
            '  return "button returned:" & (button returned of dialogResult) & linefeed & '
            '"gave up:" & ((gave up of dialogResult) as text)\n'
            "end run\n"
        )

    def _native_deny_or_legacy_result(
        self,
        native_options: list[dict[str, Any]],
        message: str,
        decision_source: str,
    ) -> DeliveryResult:
        deny = next(
            (
                option
                for option in native_options
                if option.get("selectable", True) is not False
                and str(option.get("kind") or "") == "deny"
            ),
            None,
        )
        if deny is not None:
            return DeliveryResult(
                True,
                message,
                f"dialog:{_INPUT_EVENT}",
                {
                    "action": "permission_option",
                    "decision_source": decision_source,
                    "option_handle": str(deny.get("handle") or ""),
                    "option_kind": "deny",
                },
            )
        return DeliveryResult(
            True,
            message,
            f"dialog:{_INPUT_EVENT}",
            {"action": "deny", "decision_source": decision_source},
        )

    def _send_task_elevation_dialog(
        self,
        clean: dict,
        title: str,
        dialog_timeout_s: int,
    ) -> DeliveryResult:
        """Show the v3 View Details / Deny / Approve Full decision view.

        The detail view is read-only navigation.  It has no permission
        option, no native grant, and no response receipt; only the first-level
        Deny or Approve Full button returns an actionable decision.
        """
        summary = str(clean.get("reason_summary") or clean.get("message") or "").strip()
        detail = str(clean.get("reason_detail") or summary).strip()
        deadline_at = str(clean.get("deadline_at") or "")
        identity_task_id = str(clean.get("identity_task_id") or clean.get("task_id") or "").strip()
        identity_title = str(clean.get("identity_task_title") or "").strip()
        identity_executor = str(clean.get("identity_executor") or "unknown").strip()
        identity_blocked_step = str(clean.get("identity_blocked_step") or "").strip()
        identity_lines = [
            line
            for line in (
                f"Task: {identity_task_id}" if identity_task_id else "",
                f"Title: {identity_title}" if identity_title else "",
                f"Blocked step: {identity_blocked_step}" if identity_blocked_step else "",
                "Permission scope: full",
                f"Executor: {identity_executor}",
            )
            if line
        ]
        decision_body = "\n".join([*identity_lines, "", summary])
        while True:
            give_up_s = self._permission_give_up_seconds(deadline_at, dialog_timeout_s)
            if give_up_s <= 0:
                return self._elevation_denial_result(
                    "permission dialog timed out; request denied", "timeout"
                )
            result = self._run_script(
                title,
                decision_body,
                self._task_elevation_decision_script(give_up_s),
                give_up_s,
            )
            if result is None:
                return self._elevation_denial_result(
                    "permission dialog closed; request denied", "dialog_closed"
                )
            if result == "timed_out":
                return self._elevation_denial_result(
                    "permission dialog timed out; request denied", "timeout"
                )
            if isinstance(result, DeliveryResult):
                return result
            button = str(result).strip()
            if button == "Approve Full":
                return DeliveryResult(
                    True,
                    "dialog shown; button=Approve Full; gave_up=false",
                    f"dialog:{_INPUT_EVENT}",
                    {"action": "approve_full", "decision_source": "user"},
                )
            if button == "Deny":
                return self._elevation_denial_result(
                    "dialog shown; button=Deny; gave_up=false", "user"
                )
            # View Details is deliberately UI-only.  Back re-opens the same
            # first-level dialog and emits no control-plane input.
            detail_give_up_s = self._permission_give_up_seconds(
                deadline_at, dialog_timeout_s
            )
            if detail_give_up_s <= 0:
                return self._elevation_denial_result(
                    "permission detail timed out; request denied", "timeout"
                )
            detail_result = self._run_script(
                title,
                detail,
                self._permission_detail_script(detail_give_up_s),
                detail_give_up_s,
            )
            if detail_result == "timed_out":
                return self._elevation_denial_result(
                    "permission detail timed out; request denied", "timeout"
                )
            if detail_result is None:
                return self._elevation_denial_result(
                    "permission detail closed; request denied", "dialog_closed"
                )
            if isinstance(detail_result, DeliveryResult):
                return detail_result
            if str(detail_result).strip() == "Back":
                continue
            return self._elevation_denial_result(
                "permission detail closed; request denied", "dialog_closed"
            )

    def _send_live_claude_elevation_dialog(
        self,
        clean: dict,
        title: str,
        dialog_timeout_s: int,
    ) -> DeliveryResult:
        """Show the standard full-elevation UI for a live Claude callback.

        ``View Details`` and ``Back`` are navigation-only.  ``Approve Full``
        deliberately returns the live protocol's existing ``approve`` action,
        which Runner answers against the already-pending native
        ``can_use_tool`` request in the same Claude session.  It must not be
        confused with the non-live ``approve_full`` continuation action.
        """
        summary = str(clean.get("reason_summary") or clean.get("message") or "").strip()
        detail = str(clean.get("reason_detail") or summary).strip()
        deadline_at = str(clean.get("deadline_at") or "")
        identity_lines = [
            line
            for line in (
                f"Task: {str(clean.get('identity_task_id') or clean.get('task_id') or '').strip()}",
                f"Executor: {str(clean.get('identity_executor') or 'claude').strip()}",
                f"Blocked step: {str(clean.get('identity_blocked_step') or '').strip()}",
                "Permission scope: full",
            )
            if line.split(": ", 1)[-1]
        ]
        body = "\n".join([*identity_lines, "", summary])
        while True:
            give_up_s = self._permission_give_up_seconds(deadline_at, dialog_timeout_s)
            if give_up_s <= 0:
                return self._elevation_denial_result(
                    "permission dialog timed out; request denied", "timeout"
                )
            result = self._run_script(
                title,
                body,
                self._live_claude_elevation_script(give_up_s),
                give_up_s,
            )
            if result is None:
                return self._elevation_denial_result(
                    "permission dialog closed; request denied", "dialog_closed"
                )
            if result == "timed_out":
                return self._elevation_denial_result(
                    "permission dialog timed out; request denied", "timeout"
                )
            if isinstance(result, DeliveryResult):
                return result
            button = str(result).strip()
            if button == "Approve Full":
                return DeliveryResult(
                    True,
                    "dialog shown; button=Approve Full; gave_up=false",
                    f"dialog:{_INPUT_EVENT}",
                    {"action": "approve", "decision_source": "user"},
                )
            if button == "Deny":
                return self._elevation_denial_result(
                    "dialog shown; button=Deny; gave_up=false", "user"
                )
            if button != "View Details":
                return self._elevation_denial_result(
                    f"dialog shown; button={button or 'unknown'}; gave_up=false",
                    "fail_closed",
                )

            # Details are local UI navigation only.  Recompute against the
            # original deadline before returning to the decision view.
            detail_give_up_s = self._permission_give_up_seconds(
                deadline_at, dialog_timeout_s
            )
            if detail_give_up_s <= 0:
                return self._elevation_denial_result(
                    "permission detail timed out; request denied", "timeout"
                )
            detail_result = self._run_script(
                title,
                detail,
                self._permission_detail_script(detail_give_up_s),
                detail_give_up_s,
            )
            if detail_result == "timed_out":
                return self._elevation_denial_result(
                    "permission detail timed out; request denied", "timeout"
                )
            if detail_result is None:
                return self._elevation_denial_result(
                    "permission detail closed; request denied", "dialog_closed"
                )
            if isinstance(detail_result, DeliveryResult):
                return detail_result
            if str(detail_result).strip() == "Back":
                continue
            return self._elevation_denial_result(
                "permission detail closed; request denied", "dialog_closed"
            )

    def _live_claude_elevation_script(self, give_up_s: int) -> str:
        """Return the standard full-elevation buttons for live Claude."""
        return (
            "on run argv\n"
            "  set dialogResult to display dialog (item 2 of argv) "
            'with title (item 1 of argv) buttons {"View Details", "Deny", "Approve Full"} '
            f'default button "Deny" giving up after {give_up_s} with icon caution\n'
            '  return "button returned:" & (button returned of dialogResult) & linefeed & '
            '"gave up:" & ((gave up of dialogResult) as text)\n'
            "end run\n"
        )

    def _task_elevation_decision_script(self, give_up_s: int) -> str:
        """Return the exact v3 first-level button set."""
        return (
            "on run argv\n"
            "  set dialogResult to display dialog (item 2 of argv) "
            'with title (item 1 of argv) buttons {"View Details", "Deny", "Approve Full"} '
            f'default button "Deny" giving up after {give_up_s} with icon caution\n'
            '  return "button returned:" & (button returned of dialogResult) & linefeed & '
            '"gave up:" & ((gave up of dialogResult) as text)\n'
            "end run\n"
        )

    def _elevation_denial_result(self, message: str, source: str) -> DeliveryResult:
        return DeliveryResult(
            True,
            message,
            f"dialog:{_INPUT_EVENT}",
            {"action": "deny", "decision_source": source},
        )

    def _permission_detail_script(self, give_up_s: int) -> str:
        dialog = (
            'buttons {"Back"} default button "Back" '
            f"giving up after {give_up_s} with icon note"
        )
        return (
            "on run argv\n"
            "  set dialogResult to display dialog (item 2 of argv) "
            f"with title (item 1 of argv) {dialog}\n"
            '  return "button returned:" & (button returned of dialogResult) & linefeed & '
            '"gave up:" & ((gave up of dialogResult) as text)\n'
            "end run\n"
        )

    def _permission_give_up_seconds(self, deadline_at: str, dialog_timeout_s: int) -> int:
        """Return the dialog countdown bounded by the original absolute deadline.

        The dialog never exceeds its own input timeout and never outlives the
        total deadline, so the ``View Details`` flow cannot reset the original
        countdown.  An expired deadline (or one with less than one full second
        remaining) returns ``0`` so the caller can fail closed before showing or
        re-showing any decision/detail view.
        """
        from datetime import datetime, timezone

        if not deadline_at:
            return max(dialog_timeout_s, 1)
        try:
            deadline = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
        except ValueError:
            return max(dialog_timeout_s, 1)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        remaining_s = (deadline - datetime.now(timezone.utc)).total_seconds()
        bounded = min(int(remaining_s), dialog_timeout_s)
        if bounded < 1:
            return 0
        return bounded

    def _dialog_script(self, event_type: str, input_type: str, timeout_s: int) -> str:
        if event_type == _INPUT_EVENT and input_type == "permission":
            dialog = (
                'buttons {"Deny", "Approve"} default button "Deny" '
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
        if input_type == "permission":
            if gave_up:
                return "deny"
            if button == "Approve":
                return "approve"
            if button == "View Details":
                return "view_details"
            if button == "Choose Permission":
                return "choose_permission"
            return "deny"
        if gave_up or button in {"Later", "unknown"}:
            return "dismissed"
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
