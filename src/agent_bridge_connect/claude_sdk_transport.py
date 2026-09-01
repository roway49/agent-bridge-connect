"""Official Claude Agent SDK permission control transport (PERM-104-002).

``ClaudeSDKControlTransport`` wraps the OFFICIAL ``claude_agent_sdk``
``ClaudeSDKClient`` (never the raw stream-json wire, never a fabricated
broker, never a shell ``--permission-prompt-tool`` value) and keeps its async
client alive on a dedicated worker event-loop thread so:

* the Claude process and session stay alive while an approval is pending —
  ``poll()`` exposes ``input_required`` without ending the process/session;
* ``can_use_tool`` requests are bridged into the frozen
  :class:`~agent_bridge_connect.control.ApprovalControlPlane` with the full
  approval identity (task, run, official session, ``tool_use_id``,
  fingerprint, domain, profile digest);
* Approve returns ``PermissionResultAllow(updated_input=original_input)``;
  Deny returns ``PermissionResultDeny``;
* transport death, timeouts, duplicates, and Desktop/CLI decision races
  converge and permanently invalidate stale requests.

Fail-closed guarantees (PERM-104-002):

* production never trusts callback text, stderr, prose, exit status, the raw
  ``ClaudePermissionPromptBroker``, shell ``--permission-prompt-tool``
  values, CLI continuation, or a second worker as permission evidence;
* only structured SDK ``can_use_tool`` callbacks (which carry the official
  non-empty ``tool_use_id``) create permission inputs;
* one single-action approval may wait at a time; a concurrent second request
  fails closed;
* a decision recorded against a different native ``tool_use_id`` is never
  returned as ``allow``;
* when the transport dies while a request is pending, the request is
  invalidated on the control plane and can never be reused after restart.

GGQN-002 runtime-verification contract:

* the durable ``agentbc.permission_runtime`` ``verified`` transition is
  driven ONLY by an exact structured ``PostToolUse`` success event from the
  same official SDK session and run: ``capture_tool_event`` records every
  tool-lifecycle event and ``select_verification_event`` binds the single
  eligible event to the run's verification anchor;
* for a safe/native single-action approval the anchor is the exact approved
  ``can_use_tool`` identity; its matching ``PostToolUse`` success verifies;
* for explicit full and inherited full (bypassPermissions, therefore no
  approval request) and for temporary full (consumed grant, session-scoped
  ``setMode``), the anchor is the declared action identity — the durable
  run/grant binding — and the successful target-tool event bound to it
  verifies WITHOUT inventing an approval;
* wrong, missing, duplicate, failed, unrelated, or cross-run tool events
  fail closed and never verify; PreToolUse, ``PostToolUseFailure``,
  callback prose, stderr, exit status, and a bare ``ResultMessage`` are
  never success evidence on their own.

GGQN-002 durable temporary-full revocation:

* ``_revoke_consumed_grant`` lands through the bound durable callback (the
  executor's TaskService store write) exactly once at terminal state;
  persistence failures raise ``claude_sdk_grant_revoke_failed`` instead of
  being swallowed as best-effort.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable

from agent_bridge_connect.approval import (
    core_bounded_summary,
    new_request_id,
)
from agent_bridge_connect.control import (
    ApprovalControlPlane,
    ControlPlaneError,
)
from agent_bridge_connect.session import SessionRecoveryRequired

# The frozen mapping from the AgentBC full-mode contract flag to the official
# SDK permission mode.  Only the AgentBC-generated flag value is ever mapped;
# callback text, stderr, prose and exit status can never select a mode.
SDK_PERMISSION_MODE_BY_FLAG = {
    "--dangerously-skip-permissions": "bypassPermissions",
}

# The official session-scoped PermissionUpdate applied by a trusted temporary
# full run inside the same live SDK process/session (PERM-104-002): the mode
# flip is an official ``setMode`` update with ``destination="session"``, so it
# never outlives the session and never widens the persisted settings.
SDK_SESSION_MODE_UPDATE: dict[str, str] = {
    "type": "setMode",
    "mode": "bypassPermissions",
    "destination": "session",
}

# PERM-104-002 v2: exact native choice kinds the SDK can_use_tool callback
# supports.  Deny -> PermissionResultDeny; once -> PermissionResultAllow with
# the original input and NO updated_permissions; session -> PermissionResultAllow
# whose updated_permissions is EXACTLY the callback's own suggestion bundle,
# accepted only when every suggestion is a fully valid destination="session"
# rule update (no persistent destination, no setMode bypassPermissions).
# The retired matcher-grammar constants are gone; historical receipts stay
# audit-only readable via the service projection.
SDK_V2_SESSION_DESTINATION = "session"
SDK_V2_BYPASS_MODE = "bypassPermissions"
SDK_V2_RULE_UPDATE_TYPES = frozenset({"addRules", "replaceRules"})

# The transport marks worker threads so a second worker can be detected and
# refused deterministically in tests and diagnostics.
_WORKER_THREAD_PREFIX = "agentbc-claude-sdk-"

TransportDeathCallback = Callable[[str], None]


def _permission_update_to_dict(update: Any) -> dict[str, Any] | None:
    """Convert one SDK PermissionUpdate dataclass to its exact dict shape."""
    if update is None:
        return None
    if isinstance(update, dict):
        return dict(update)
    data = getattr(update, "__dict__", None)
    if isinstance(data, dict):
        return {
            key: value
            for key, value in data.items()
            if value is not None and not str(key).startswith("_")
        }
    return None


def _permission_rule_to_dict(rule: Any) -> dict[str, Any] | None:
    """Convert one SDK PermissionRuleValue to its exact dict shape."""
    if rule is None:
        return None
    if isinstance(rule, dict):
        return dict(rule)
    data = getattr(rule, "__dict__", None)
    if isinstance(data, dict):
        return {
            key: value
            for key, value in data.items()
            if value is not None and not str(key).startswith("_")
        }
    return None


def _permission_update_from_dict(value: dict[str, Any]) -> Any:
    """Rebuild one exact SDK PermissionUpdate from its captured dict shape."""
    from claude_agent_sdk import PermissionUpdate
    from claude_agent_sdk.types import PermissionRuleValue

    rules = value.get("rules")
    rule_values: list[Any] = []
    if isinstance(rules, (list, tuple)):
        for rule in rules:
            rule_dict = _permission_rule_to_dict(rule)
            if rule_dict is None:
                continue
            rule_values.append(
                PermissionRuleValue(
                    tool_name=str(rule_dict.get("tool_name") or ""),
                    rule_content=rule_dict.get("rule_content"),
                )
            )
    return PermissionUpdate(
        type=str(value.get("type") or ""),
        rules=rule_values or None,
        behavior=value.get("behavior"),
        mode=value.get("mode"),
        directories=value.get("directories"),
        destination=value.get("destination"),
    )

# Durable grant-revocation callback: invoked exactly once with
# ``(grant, revocation_code)`` at the transport's terminal state.  Production
# binds the executor's TaskService-backed store revocation here so consumed
# temporary-full grants survive crash/recovery; the transport never mints or
# widens grants.
GrantRevokeCallback = Callable[[dict[str, Any], str], None]


class ClaudeSDKTransportError(RuntimeError):
    """Transport-level failure carrying a stable AgentBC code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.code = str(code or "claude_sdk_transport_error")
        self.details = dict(details or {})
        super().__init__(message)


class ClaudeSDKControlTransport:
    """Bind the official SDK ``can_use_tool`` callback to the ControlPlane.

    The transport is executor-agnostic about the rest of AgentBC: it needs a
    live :class:`ApprovalControlPlane` and the bound approval identity.  The
    SDK client itself is started lazily by :meth:`start` on a dedicated
    worker thread whose event loop stays alive until :meth:`stop`.
    """

    def __init__(
        self,
        *,
        plane: ApprovalControlPlane,
        task_id: str,
        run_id: str,
        session_id: str,
        executor: str = "claude",
        approval_timeout_s: float = 300.0,
        escalation_domain: str = "",
        host_profile_digest: str = "",
        grant_revoke_callback: GrantRevokeCallback | None = None,
    ) -> None:
        self.plane = plane
        self.task_id = str(task_id or "").strip()
        self.run_id = str(run_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.executor = str(executor or "claude").strip().lower()
        self.approval_timeout_s = max(float(approval_timeout_s), 0.1)
        self.escalation_domain = str(escalation_domain or "").strip().lower()
        self.host_profile_digest = str(host_profile_digest or "").strip()
        if self.executor == "claude":
            # The production executor supplies these frozen facts.  Direct
            # transport callers still receive the same bounded defaults so a
            # native request can never omit its domain/profile binding.
            if not self.escalation_domain:
                self.escalation_domain = "executor_policy"
            if not self.host_profile_digest:
                from agent_bridge_connect.permission_runtime import host_profile_digest

                self.host_profile_digest = host_profile_digest()
        # PERM-104-002 (GGQN-002): the durable revocation path for consumed
        # temporary-full grants.  Production binds the TaskService store
        # callback; without it a consumed grant cannot be durably revoked and
        # :meth:`_revoke_consumed_grant` fails closed with a stable error.
        self._grant_revoke_callback = grant_revoke_callback
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: threading.Thread | None = None
        self._worker_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._closed = False
        self._pending_tool_use_ids: set[str] = set()
        self._pending_lock = threading.Lock()
        self._last_error: dict[str, Any] | None = None
        # Live client context handle for driver-level exit handling.
        self._client_context: Any = None
        # Every tool_use_id ever accepted on this transport: a duplicate
        # native identity can never create a second permission input.
        self._seen_tool_use_ids: set[str] = set()
        # PERM-104-002 v2: the callback's own permission suggestions for the
        # in-flight request, captured verbatim so a session choice can return
        # EXACTLY the offered bundle.  Keyed by AgentBC request id.
        self._pending_session_suggestions: dict[str, Any] = {}
        # The tool_use_id of the one in-flight single-action approval (or the
        # empty string): the concurrency gate and death-invalidation anchor.
        self._active_request = ""
        # The durable ControlPlane request id paired with the active native
        # tool identity.  These are deliberately separate: item/tool ids are
        # SDK identities, while request ids are AgentBC response-file keys.
        self._active_request_id = ""
        # PERM-104-002 temporary-full lifecycle: the grant envelope consumed
        # for this run, revoked durably on terminal/crash/handoff/reassign.
        self._consumed_grant: dict[str, Any] | None = None
        # PERM-104-002 (GGQN-002): structured PostToolUse success events
        # captured on this exact SDK session/run.  Each entry carries the
        # native tool_use_id, the official session id, the tool name, and the
        # monotonic arrival order; verification is driven from these events,
        # never from callback prose, stderr, exit status, or ResultMessage.
        self._tool_events: list[dict[str, Any]] = []
        self._tool_events_lock = threading.Lock()
        self._tool_event_count = 0
        # The single anchor identity this run may verify under and its mode:
        # "approved_tool_use" (exact approved can_use_tool id for single
        # actions) or "declared_run" (declared action identity for the
        # pre-authorized full modes).  A PostToolUse event that does not
        # satisfy the anchor contract fails closed.
        self._verification_anchor = ""
        self._anchor_mode = ""
        # PERM-104-002: the approved request's structured escalation binding,
        # recorded when the anchor is set so the executor's runtime
        # verification can reconcile the exact ledger entry with the
        # anchor's PostToolUse success (execution_result="succeeded").
        self._anchor_action_fingerprint = ""
        self._anchor_escalation_domain = ""
        self._anchor_profile_digest = ""
        # Set once the driver finished consuming the session stream: only
        # PostToolUse events observed inside this exact run window are
        # eligible (a replayed foreign event fails closed).
        self._stream_consumed = threading.Event()

    # ── worker event-loop lifecycle ────────────────────────────────────────

    def start(self) -> None:
        """Start the worker event-loop thread that owns the SDK client."""
        if self._worker is not None:
            raise ClaudeSDKTransportError(
                "claude_sdk_transport_already_started",
                "The Claude SDK transport worker is already running.",
            )
        self._stop_requested.clear()
        self._worker = threading.Thread(
            target=self._run_worker,
            name=f"{_WORKER_THREAD_PREFIX}{self.run_id}",
            daemon=True,
        )
        self._worker.start()
        if not self._worker_ready.wait(timeout=10.0):
            raise ClaudeSDKTransportError(
                "claude_sdk_transport_start_failed",
                "The Claude SDK transport worker did not start in time.",
            )

    def _run_worker(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._worker_ready.set()
        try:
            # run_forever keeps the loop (and therefore the SDK client's
            # streams, timers, and pending can_use_tool futures) alive while
            # the main thread polls; a pending approval never ends the
            # process or the session.
            loop.run_forever()
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001 - shutdown best-effort.
                pass
            loop.close()

    def _submit(self, coro_factory: Callable[[], Any], timeout_s: float) -> Any:
        """Run one awaitable on the worker loop and wait for its result."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise ClaudeSDKTransportError(
                "claude_sdk_transport_dead",
                "The Claude SDK transport worker loop is not running.",
            )
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        try:
            return future.result(timeout=timeout_s)
        except TimeoutError as exc:
            future.cancel()
            raise ClaudeSDKTransportError(
                "claude_sdk_transport_timeout",
                "The Claude SDK transport operation timed out.",
            ) from exc

    def stop(self, timeout_s: float = 5.0) -> None:
        """Disconnect the SDK client and stop the worker loop."""
        if self._closed:
            return
        self._closed = True
        client = self._client
        if client is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    client.disconnect(), self._loop
                ).result(timeout=timeout_s)
            except Exception:  # noqa: BLE001 - shutdown best-effort.
                pass
        self._stop_requested.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout_s)
        self._loop = None
        self._worker = None
        self._client = None

    # ── SDK client lifecycle ───────────────────────────────────────────────

    def connect_client(self, client: Any) -> None:
        """Adopt an already-connected SDK client (called on the worker loop).

        Production passes a live ``ClaudeSDKClient`` created with
        ``ClaudeAgentOptions(can_use_tool=self.can_use_tool, ...)``.  Tests
        may pass a stub exposing the same ``query``/``receive_response``/
        ``disconnect`` surface.
        """
        with self._client_lock:
            if self._client is not None:
                raise ClaudeSDKTransportError(
                    "claude_sdk_client_duplicate",
                    "A second SDK client was attached to one transport; "
                    "AgentBC never runs a second worker.",
                )
            self._client = client

    async def connect_client_async(self, client: Any) -> None:
        """Async variant used inside the worker loop (connect then adopt)."""
        connect = getattr(client, "connect", None)
        if connect is not None:
            await connect()
        self.connect_client(client)

    @property
    def client(self) -> Any:
        return self._client

    def is_alive(self) -> bool:
        worker = self._worker
        return (
            worker is not None
            and worker.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        )

    # ── can_use_tool bridge ────────────────────────────────────────────────

    async def can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> Any:
        """The official SDK callback, bridged to the frozen ControlPlane.

        Runs on the worker loop inside the SDK's own permission plumbing; the
        returned coroutine result is the official ``PermissionResult`` the
        SDK hands back to the CLI on the same process/session.  A concurrent
        second ``can_use_tool`` fails closed instead of queueing behind the
        first, so one dialog can never authorize two actions.
        """
        from agent_bridge_connect.permission_transport import (
            CONTROL_PATH_SDK_TRANSPORT,
        )

        tool_use_id = str(getattr(context, "tool_use_id", "") or "").strip()
        tool = str(tool_name or "").strip() or "unknown"
        if not tool_use_id:
            # The SDK wire guarantees a non-empty tool_use_id for
            # can_use_tool; absence means the request is not trustworthy.
            return self._deny_result(
                "AgentBC could not verify this tool call identity; "
                "the action was denied (claude_sdk_tool_use_id_missing)."
            )
        with self._pending_lock:
            if (
                tool_use_id in self._seen_tool_use_ids
                or tool_use_id in self._pending_tool_use_ids
                or self._active_request
            ):
                return self._deny_result(
                    "A duplicate or concurrent tool call identity was "
                    "rejected (claude_sdk_tool_use_id_duplicate)."
                )
            self._seen_tool_use_ids.add(tool_use_id)
            self._pending_tool_use_ids.add(tool_use_id)
            self._active_request = tool_use_id
        try:
            # The bridge is awaited (not the blocking helper): the worker
            # loop stays responsive and the SDK client keeps streaming while
            # the human decision is pending on the same process/session.
            return await self._request_and_wait(
                CONTROL_PATH_SDK_TRANSPORT,
                tool,
                tool_use_id,
                dict(input_data or {}),
                context,
            )
        finally:
            with self._pending_lock:
                self._pending_tool_use_ids.discard(tool_use_id)
                if self._active_request == tool_use_id:
                    self._active_request = ""
                    self._active_request_id = ""
                self._pending_session_suggestions.pop(self._active_request_id, None)

    def _deny_result(self, message: str) -> Any:
        from claude_agent_sdk import PermissionResultDeny

        return PermissionResultDeny(message=str(message or ""))

    def _allow_result(self, original_input: dict[str, Any]) -> Any:
        from claude_agent_sdk import PermissionResultAllow

        return PermissionResultAllow(updated_input=dict(original_input or {}))

    def validate_session_bundle(
        self, suggestions: Any
    ) -> list[dict[str, Any]] | None:
        """Return the suggestion bundle only when it is fully session-valid.

        PERM-104-002 v2: the session choice exists ONLY when the callback's
        current suggestions are a fully valid addRules/replaceRules allow
        bundle.  An absent destination means the CLI is waiting for the user's
        scope choice and is mechanically bound to ``destination="session"``;
        an explicit session destination is preserved.  No persistent destination
        (userSettings/projectSettings/localSettings), no setMode (and
        therefore never bypassPermissions), no add/removeDirectories.  Any
        unknown or mixed shape returns ``None`` so the session choice is not
        offered.  AgentBC never infers or regenerates rule content.
        """
        if not isinstance(suggestions, (list, tuple)) or not suggestions:
            return None
        bundle: list[dict[str, Any]] = []
        for suggestion in suggestions:
            update = (
                suggestion
                if isinstance(suggestion, dict)
                else _permission_update_to_dict(suggestion)
            )
            if not isinstance(update, dict):
                return None
            update_type = str(update.get("type") or "")
            if update_type not in SDK_V2_RULE_UPDATE_TYPES:
                return None
            behavior = str(update.get("behavior") or "")
            if behavior != "allow":
                return None
            destination = update.get("destination")
            if destination is not None and str(destination) != SDK_V2_SESSION_DESTINATION:
                return None
            rules = update.get("rules")
            if not isinstance(rules, (list, tuple)) or not rules:
                return None
            for rule in rules:
                rule_dict = (
                    rule
                    if isinstance(rule, dict)
                    else _permission_rule_to_dict(rule)
                )
                if not isinstance(rule_dict, dict):
                    return None
                if not str(rule_dict.get("tool_name") or "").strip():
                    return None
            # The CLI's mechanical suggestion may omit destination because
            # scope is the user's pending UI choice.  Preserve the exact rule
            # and behavior, and bind only its destination to this live session.
            # No matcher, tool category, or rule content is inferred here.
            session_update = dict(update)
            session_update["destination"] = SDK_V2_SESSION_DESTINATION
            bundle.append(session_update)
        return bundle

    def _session_bundle_allow_result(
        self,
        original_input: dict[str, Any],
        bundle: list[dict[str, Any]],
    ) -> Any:
        """Allow carrying EXACTLY the callback's own session bundle.

        The bundle validated by :meth:`validate_session_bundle` is returned
        verbatim as ``updated_permissions``; AgentBC never generates rules of
        its own.
        """
        from claude_agent_sdk import PermissionResultAllow

        updates: list[Any] = []
        for update in bundle:
            if isinstance(update, dict):
                updates.append(_permission_update_from_dict(update))
            else:
                updates.append(update)
        return PermissionResultAllow(
            updated_input=dict(original_input or {}),
            updated_permissions=updates,
        )

    async def _request_and_wait(
        self,
        control_path: str,
        tool: str,
        tool_use_id: str,
        tool_input: dict[str, Any],
        context: Any = None,
    ) -> Any:
        """Bridge one SDK permission request into the frozen ControlPlane.

        The approval identity binds task, run, official session, the native
        ``tool_use_id``, the tool fingerprint, the escalation domain and the
        host profile digest.  A decision recorded against a different native
        request is never returned as ``allow``.
        """
        from agent_bridge_connect.approval import compute_request_fingerprint
        from agent_bridge_connect.control import claude_offered_choices

        request_id = new_request_id()
        with self._pending_lock:
            if self._active_request == tool_use_id:
                self._active_request_id = request_id
        # PERM-104-002 v2: capture the callback's own suggestions verbatim.
        # The session choice is offered ONLY when the current suggestions are
        # a fully valid destination=session bundle; persistent/unknown shapes
        # never widen the offered choice set.
        raw_suggestions = getattr(context, "suggestions", None)
        session_bundle = self.validate_session_bundle(raw_suggestions)
        with self._pending_lock:
            if session_bundle is not None:
                self._pending_session_suggestions[request_id] = raw_suggestions
        fingerprint = compute_request_fingerprint(
            executor=self.executor,
            session_id=self.session_id,
            tool_name=tool,
            tool_input=tool_input,
            extra={"tool_use_id": tool_use_id},
        )
        # Stable action identity excludes the native tool_use_id, which is a
        # per-attempt transport identity.  The same blocked command retried
        # under a fresh SDK call id must converge, while a different command
        # in the same Bash tool must remain independently approvable.
        action_fingerprint_value = compute_request_fingerprint(
            executor=self.executor,
            session_id=self.session_id,
            tool_name=tool,
            tool_input=tool_input,
        )
        summary = core_bounded_summary(executor=self.executor, operation=tool)
        offered = claude_offered_choices(session_bundle_supported=session_bundle is not None)
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": self.session_id,
                "turnId": "",
                "itemId": tool_use_id,
                "reason": summary,
            },
            # Identity fields are top level so ``normalize_approval_request``
            # reads them directly; the nested ``_agentbc`` block stays for
            # wire-level diagnostics only.
            "_agentbc": {
                "task_id": self.task_id,
                "executor_run_id": self.run_id,
                "tool_use_id": tool_use_id,
                "request_id": request_id,
                "tool_name": tool,
                "request_fingerprint": fingerprint,
                "action_fingerprint": action_fingerprint_value,
                "control_path": control_path,
                "escalation_domain": self.escalation_domain,
                "host_profile_digest": self.host_profile_digest,
            },
            "escalation_domain": self.escalation_domain,
            "host_profile_digest": self.host_profile_digest,
            # PERM-104-002 v2: the executor-native choice set.  Core offers
            # exactly these choices; no permission category is inferred.
            "approval_version": 2,
            "authority": {
                "executor": self.executor,
                "protocol": "claude_agent_sdk",
                "protocol_version": 1,
                "method": "sdk.can_use_tool",
            },
            "offered_choices": [dict(choice) for choice in offered],
        }
        try:
            event = await asyncio.to_thread(self.plane.request_approval, message)
            event_request_id = str(event.get("request_id") or "")
            decision_request_id = event_request_id or request_id
            try:
                response = await asyncio.to_thread(
                    self.plane.wait_for_decision,
                    decision_request_id,
                    self.approval_timeout_s,
                )
            except ControlPlaneError as exc:
                if exc.code in {"approval_request_expired", "approval_request_stale"}:
                    raise ClaudeSDKTransportError(
                        "claude_sdk_approval_timeout",
                        "The SDK permission request expired or was invalidated "
                        "before a decision arrived.",
                        {"request_id": decision_request_id, "tool_use_id": tool_use_id},
                    ) from exc
                raise ClaudeSDKTransportError(
                    exc.code,
                    f"The SDK permission wait failed: {exc}",
                    {"request_id": decision_request_id, "tool_use_id": tool_use_id},
                ) from exc
            decision = str(response.get("decision") or "").strip().lower()
            if decision == "accept":
                # Approve authorizes only the original action: the official
                # allow MUST carry the untouched original input.  GGQN-002:
                # the exact approved tool_use_id becomes this run's
                # verification anchor — the only identity whose structured
                # PostToolUse success may verify the runtime receipt.
                with self._pending_lock:
                    self._verification_anchor = tool_use_id
                    self._anchor_action_fingerprint = action_fingerprint_value
                    self._anchor_escalation_domain = self.escalation_domain
                    self._anchor_profile_digest = self.host_profile_digest
                # PERM-104-002 v2: the exact selected native choice decides
                # the response shape.  once -> original input, no
                # updated_permissions; session -> EXACTLY the callback's own
                # validated destination=session bundle.  The retired
                # --approve-tool matcher rules are never consulted.
                choice = response.get("choice")
                choice_kind = (
                    str(choice.get("kind") or "")
                    if isinstance(choice, dict)
                    else ""
                )
                if choice_kind == "session":
                    bundle = self.validate_session_bundle(
                        self._pending_session_suggestions.get(decision_request_id)
                    )
                    if bundle is None:
                        raise ClaudeSDKTransportError(
                            "claude_sdk_session_bundle_invalid",
                            "The session choice was selected but the callback's "
                            "suggestions are not a fully valid destination="
                            "session bundle.",
                            {"request_id": decision_request_id, "tool_use_id": tool_use_id},
                        )
                    return self._session_bundle_allow_result(tool_input, bundle)
                return self._allow_result(tool_input)
            return self._deny_result(
                "The user denied this action through AgentBC."
            )
        except (ControlPlaneError, SessionRecoveryRequired) as exc:
            raise ClaudeSDKTransportError(
                getattr(exc, "code", "claude_sdk_approval_rejected"),
                f"The control plane rejected the SDK permission request: {exc}",
                {"request_id": request_id, "tool_use_id": tool_use_id},
            ) from exc

    # ── transport-death invalidation ───────────────────────────────────────

    def record_transport_death(self, reason: str = "transport died") -> dict[str, Any]:
        """Invalidate any pending request when the transport dies.

        A dead transport can never be resumed into; recovery must mint a
        fresh run/request identity.  The in-flight request is tracked on the
        transport itself so the control plane invalidates exactly that
        request (status ``invalidated``) instead of leaving it pending
        behind a dead client.
        """
        with self._pending_lock:
            active = str(self._active_request_id or "")
        if not active:
            pending = self.plane.status().get("pending_request")
            if isinstance(pending, dict) and pending.get("status") == "pending":
                active = str(pending.get("request_id") or "")
        return self.plane.record_transport_failed(
            f"Claude SDK {reason}",
            request_id=active,
            evidence={"run_id": self.run_id, "executor": self.executor},
        )

    def status(self) -> dict[str, Any]:
        """Redacted diagnostics for projections (no tool input, no argv)."""
        return {
            "transport": "claude_sdk_control_transport",
            "run_id": self.run_id,
            "session_bound": bool(self.session_id),
            "alive": self.is_alive(),
            "pending_tool_use_ids": len(self._pending_tool_use_ids),
            "pending_request": (
                self.plane.status().get("pending_request")
                if self._pending_tool_use_ids
                else None
            ),
            "approval_timeout_s": self.approval_timeout_s,
        }

    # ── structured tool-event capture (verification evidence) ─────────────

    def attach_session_rule(self, rule: dict[str, Any]) -> None:
        """Retired tombstone (PERM-104-002 1.04A).

        The legacy CLI matcher rules were removed.  Any caller still trying
        to attach one fails closed instead of applying a rule.
        """
        raise ClaudeSDKTransportError(
            "legacy_session_tool_rule_removed",
            "Session tool rules were removed; permissions are granted only "
            "through the executor-native choice broker.",
        )

    def session_rule_attached(self) -> bool:
        return False

    def approved_action_binding(self) -> dict[str, Any]:
        """Return the approved anchor's structured escalation binding.

        Empty ``action_fingerprint`` means no single-action approval is
        anchored on this transport.  Used by the executor's runtime
        verification to reconcile the exact block-ledger entry with the
        anchor's structured PostToolUse success.
        """
        with self._pending_lock:
            return {
                "tool_use_id": self._verification_anchor,
                "action_fingerprint": self._anchor_action_fingerprint,
                "escalation_domain": self._anchor_escalation_domain,
                "profile_digest": self._anchor_profile_digest,
            }

    def _session_rule_from_receipt(self) -> dict[str, Any] | None:
        """Retired: always ``None`` (audit-only; no rule is ever attached)."""
        return None

    def capture_tool_event(
        self,
        *,
        event: str,
        tool_use_id: str,
        tool_name: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Record one structured SDK tool-lifecycle event for this run.

        The SDK ``hooks`` feed (``build_sdk_hooks``) calls this for every
        PreToolUse/PostToolUse/PostToolUseFailure observed on this exact
        session.  Events are appended with a monotonic sequence so duplicate
        or replayed identities are detectable.  A ``session_id`` that does
        not match this transport's bound session is recorded with its own id
        and can never satisfy :meth:`select_verification_event` (fail closed
        against cross-run replays).  Events observed after the run's stream
        window closed are marked out-of-window and never verify.
        """
        normalized_event = str(event or "").strip()
        if normalized_event not in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            return {"accepted": False, "reason": "claude_sdk_tool_event_unknown"}
        in_window = not self._stream_consumed.is_set()
        with self._tool_events_lock:
            self._tool_event_count += 1
            record = {
                "sequence": self._tool_event_count,
                "event": normalized_event,
                "tool_use_id": str(tool_use_id or "").strip(),
                "tool_name": str(tool_name or "").strip(),
                "session_id": str(session_id or "").strip() or self.session_id,
                "transport_session_id": self.session_id,
                "run_id": self.run_id,
                "in_run_window": in_window,
            }
            self._tool_events.append(record)
        return {"accepted": True, "sequence": record["sequence"]}

    def declare_run_authorization(
        self,
        *,
        mode: str,
        identity: str = "",
    ) -> None:
        """Declare the run-level authorization this run may verify under.

        GGQN-002: explicit full and inherited full run with
        ``bypassPermissions`` and therefore never produce a ``can_use_tool``
        approval request.  Their verification anchor is the DECLARED action
        identity of the pre-authorized run (Runner dispatch
        ``runner-dispatch-{run_id}`` on the frozen full base) — never an
        invented approval.  Temporary full declares the consumed grant's
        durable binding identity via :meth:`attach_consumed_grant`; safe
        single-action runs take the exact approved ``can_use_tool`` identity
        automatically on approve.
        """
        normalized = str(mode or "").strip()
        if normalized not in ("declared_run", "approved_tool_use"):
            raise ClaudeSDKTransportError(
                "claude_sdk_verification_anchor_invalid",
                "Unsupported verification anchor mode.",
            )
        with self._pending_lock:
            self._anchor_mode = normalized
            self._verification_anchor = str(identity or "").strip() or (
                f"run:{self.run_id}" if normalized == "declared_run" else ""
            )
            if not self._verification_anchor:
                raise ClaudeSDKTransportError(
                    "claude_sdk_verification_anchor_invalid",
                    "A single-action verification anchor requires the exact "
                    "approved tool_use_id.",
                )

    def select_verification_event(
        self,
        *,
        session_id: str = "",
        tool_name: str = "",
    ) -> dict[str, Any] | None:
        """Select the exact PostToolUse success event backing verification.

        GGQN-002 verification contract — the selected event must be, in the
        same transport session and run window:

        * a ``PostToolUse`` success (never ``PostToolUseFailure``, never
          ``PreToolUse``);
        * bound to this transport's verification anchor — the exact approved
          ``can_use_tool`` tool_use_id for single-action approvals, or the
          declared run/grant action identity for the pre-authorized full
          modes (never an invented approval);
        * observed exactly once in this run window (duplicate or cross-run
          identities never verify) and never carrying a failure twin;
        * carrying this transport's official session id when one is bound.

        Returns ``None`` when no event satisfies the contract.
        """
        anchor = str(self._verification_anchor or "").strip()
        if not anchor:
            # No approved request and no declared run authorization: nothing
            # was authorized, so nothing can verify.
            return None
        expected_session = str(session_id or self.session_id or "").strip()
        expected_tool = str(tool_name or "").strip()
        with self._tool_events_lock:
            events = list(self._tool_events)
        post_success = [
            event
            for event in events
            if event.get("event") == "PostToolUse"
            and event.get("in_run_window") is True
            and (not expected_tool or event.get("tool_name") == expected_tool)
        ]
        if expected_session:
            post_success = [
                event
                for event in post_success
                if str(event.get("session_id") or "") == expected_session
            ]
        if self._anchor_mode == "approved_tool_use":
            matches = [
                event
                for event in post_success
                if str(event.get("tool_use_id") or "") == anchor
            ]
            if len(matches) != 1:
                # Zero matches prove nothing; multiple matches are a
                # duplicate or replayed identity and fail closed.
                return None
            return dict(matches[0])
        # Declared-run mode (explicit full / inherited full / temporary
        # full): the bypass run's tool_use_ids are not knowable before the
        # run, so the binding is the earliest successful in-window target
        # tool event whose identity is unique in this window and carries no
        # failure twin — a wrong, duplicate, failed, or replayed event never
        # becomes the verifying evidence.
        counts: dict[str, int] = {}
        for event in post_success:
            identity = str(event.get("tool_use_id") or "")
            counts[identity] = counts.get(identity, 0) + 1
        failure_ids = {
            str(event.get("tool_use_id") or "")
            for event in events
            if event.get("event") == "PostToolUseFailure"
            and event.get("in_run_window") is True
        }
        for event in sorted(post_success, key=lambda item: int(item["sequence"])):
            identity = str(event.get("tool_use_id") or "")
            if not identity or counts.get(identity) != 1 or identity in failure_ids:
                continue
            return dict(event)
        return None

    # ── blocking session driver (executor-facing) ──────────────────────────

    def run_controlled(
        self,
        *,
        options: Any,
        prompt: str,
        timeout_s: float,
        runtime_verify_callback: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Run one full SDK session to completion and capture structured facts.

        The official client is connected on the worker loop with this
        transport's ``can_use_tool``; the prompt is sent; every message until
        (and including) the terminal ``ResultMessage`` is consumed.  The
        caller (executor ``start_control``) blocks here — exactly like the
        Codex App Server worker — while ``poll()`` on the main thread exposes
        only the final structured result; pending approvals surface through
        the ControlPlane, not through poll races.

        ``runtime_verify_callback`` (when supplied) is invoked after the
        frozen ControlPlane decision and the structured ``ResultMessage`` so
        the durable ``agentbc.permission_runtime`` record can be verified for
        the exact verification anchor.  Only the structured PostToolUse
        success event selected by :meth:`select_verification_event` can prove
        execution; PreToolUse records, failure records, callback prose,
        stderr, and exit status never verify the runtime capability.  The
        returned result carries redacted, structured facts only.

        Temporary-full lifecycle (PERM-104-002): when a consumed one-shot
        grant is attached via :meth:`attach_consumed_grant`, the run starts
        in the SDK default mode and the official session-scoped
        ``PermissionUpdate(type="setMode", mode="bypassPermissions",
        destination="session")`` is applied to the same live client before
        the prompt is sent.  The grant is durably revoked the moment this run
        reaches its terminal state — the extra capability never outlives the
        single run that consumed it, and the mode flip itself dies with the
        session.
        """
        try:
            return self._submit(
                lambda: self._run_session_async(
                    options,
                    prompt,
                    runtime_verify_callback,
                    on_started,
                ),
                timeout_s,
            )
        finally:
            # Terminal state (result returned OR timeout/crash): a consumed
            # temporary-full grant is revoked durably and exactly once.
            # Handoff and reassign run this same path when the old run is
            # superseded.
            self._revoke_consumed_grant("claude_run_terminal")

    def attach_consumed_grant(self, grant: dict[str, Any]) -> None:
        """Attach the consumed one-shot grant backing this run's full mode.

        ``grant`` must already be validated and marked ``consumed`` by the
        Runner/Service layer; the transport only mirrors it for lifecycle
        revocation and never mints or re-issues grants itself.
        """
        if str((grant or {}).get("state", {}).get("status") or "") != "consumed":
            raise ClaudeSDKTransportError(
                "claude_sdk_grant_not_consumed",
                "Only a consumed one-shot grant may back a temporary full run.",
            )
        self._consumed_grant = dict(grant)
        # GGQN-002: temporary full has no can_use_tool request (bypass
        # permissions), so the run verifies under the declared-run anchor
        # backed by the durable consumed-grant binding (grant + target run).
        self.declare_run_authorization(mode="declared_run")

    def _revoke_consumed_grant(self, code: str) -> None:
        """Durably revoke the consumed temporary-full grant exactly once.

        GGQN-002: the in-memory envelope alone is never mutated as evidence.
        Revocation lands through the bound durable callback (the executor's
        TaskService store write) and the returned revoked envelope replaces
        the mirrored copy.  Without a bound callback — or when the durable
        write fails — the transport raises ``claude_sdk_grant_revoke_failed``
        (fail closed): a consumed temporary-full grant that outlives its run
        must surface as a hard error, never as best-effort silence.
        """
        grant = self._consumed_grant
        if grant is None:
            return
        callback = self._grant_revoke_callback
        if callback is None:
            raise ClaudeSDKTransportError(
                "claude_sdk_grant_revoke_failed",
                "No durable grant-revocation callback is bound; the consumed "
                "temporary-full grant cannot be revoked for this run.",
            )
        callback(grant, str(code or "").strip())
        self._consumed_grant = None

    def _run_session_async(
        self,
        options: Any,
        prompt: str,
        runtime_verify_callback: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
        on_started: Callable[[], None] | None,
    ) -> Any:
        """Session driver coroutine: enter, drive, and exit the SDK client."""
        return self._run_session_coroutine(
            options, prompt, runtime_verify_callback, on_started
        )

    async def _run_session_coroutine(
        self,
        options: Any,
        prompt: str,
        runtime_verify_callback: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
        on_started: Callable[[], None] | None,
    ) -> dict[str, Any]:
        from claude_agent_sdk import ClaudeSDKClient

        client = ClaudeSDKClient(options)
        entered = client.__aenter__()
        # ``connect`` is a plain coroutine on the official client; support
        # both awaitable and await-returning __aenter__ implementations so
        # the driver never depends on a private SDK shape.
        client_obj = (
            await entered if hasattr(entered, "__await__") else entered
        )
        self._client_context = client
        try:
            return await self._drive_session(
                client_obj, prompt, runtime_verify_callback, on_started
            )
        finally:
            try:
                exit_result = client.__aexit__(None, None, None)
                if hasattr(exit_result, "__await__"):
                    await exit_result
            except Exception:  # noqa: BLE001 - shutdown best-effort.
                pass

    async def _drive_session(
        self,
        client: Any,
        prompt: str,
        runtime_verify_callback: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
        on_started: Callable[[], None] | None,
    ) -> dict[str, Any]:
        self.connect_client(client)
        if on_started is not None:
            try:
                on_started()
            except Exception:  # noqa: BLE001 - heartbeat failures are fatal upstream.
                pass
        session_mode_applied = await self._apply_session_mode_update(client)
        await client.query(prompt)
        stdout_parts: list[str] = []
        session_ids: set[str] = set()
        result_payload: dict[str, Any] = {}
        async for message in client.receive_response():
            kind = type(message).__name__
            if kind == "ResultMessage":
                session_id = str(getattr(message, "session_id", "") or "")
                if session_id:
                    session_ids.add(session_id)
                result_payload = {
                    "is_error": bool(getattr(message, "is_error", False)),
                    "num_turns": int(getattr(message, "num_turns", 0) or 0),
                    "session_id": session_id,
                    "result": str(getattr(message, "result", "") or ""),
                }
                text = getattr(message, "result", None)
                if isinstance(text, str):
                    stdout_parts.append(text)
        captured = {
            "stdout": "\n".join(stdout_parts),
            "stderr": "",
            "returncode": 0 if not result_payload.get("is_error") else 1,
            "init_verified": bool(session_ids),
            "session_id": sorted(session_ids)[0] if session_ids else "",
            "result": result_payload,
            "session_mode_applied": bool(session_mode_applied),
        }
        # Only events observed inside this exact run window (before the
        # ResultMessage closed the turn) are eligible for verification.
        self._stream_consumed.set()
        if runtime_verify_callback is not None:
            try:
                runtime_verify_callback(self._verification_anchor, captured)
            except Exception:  # noqa: BLE001 - verification failures surface via receipts.
                pass
        return captured

    async def _apply_session_mode_update(self, client: Any) -> bool:
        """Apply the official session-scoped temporary-full mode flip.

        When a consumed one-shot grant backs this run, the frozen
        :data:`SDK_SESSION_MODE_UPDATE` contract — the official
        ``PermissionUpdate`` shape ``type="setMode"``,
        ``mode="bypassPermissions"``, ``destination="session"`` — is applied
        to the same live client (via the official ``set_permission_mode``
        control request) BEFORE the prompt is sent, so every ask-path action
        of this run executes inside the session-scoped bypass.  The update
        is session-scoped: it never touches user/project/local settings and
        dies with the session.  The consumed grant itself is durably revoked
        by :meth:`run_controlled` on terminal/crash.

        Returns whether the session-scoped mode update was applied.
        """
        if self._consumed_grant is None:
            return False
        if (
            SDK_SESSION_MODE_UPDATE["type"] != "setMode"
            or SDK_SESSION_MODE_UPDATE["destination"] != "session"
            or SDK_SESSION_MODE_UPDATE["mode"] != "bypassPermissions"
        ):
            raise ClaudeSDKTransportError(
                "claude_sdk_session_mode_contract_invalid",
                "The frozen session-scoped mode-update contract drifted from "
                "the official PermissionUpdate shape.",
            )
        await client.set_permission_mode(SDK_SESSION_MODE_UPDATE["mode"])
        return True


def build_sdk_options(
    *,
    cli_path: str,
    cwd: str,
    can_use_tool: Callable[[str, dict[str, Any], Any], Any],
    permission_mode: str = "default",
    session_id: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    model: str | None = None,
    max_budget_usd: float | None = None,
    max_turns: int | None = None,
    hooks: dict[str, list[Any]] | None = None,
    env: dict[str, str] | None = None,
    add_dirs: list[str] | None = None,
    sandbox: dict[str, Any] | None = None,
    extra_args: dict[str, str | None] | None = None,
) -> Any:
    """Build official ``ClaudeAgentOptions`` for the control transport.

    The CLI path is always the configured absolute binary (no PATH fallback)
    and ``can_use_tool`` is always the AgentBC bridge.  ``resume`` is bound
    only when the task packet carries an official persisted session id.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = {
        "cli_path": cli_path,
        "cwd": cwd,
        "permission_mode": permission_mode,
        "can_use_tool": can_use_tool,
        "tools": list(tools or []),
        "allowed_tools": list(allowed_tools or []),
        "disallowed_tools": list(
            disallowed_tools or ["TaskCreate", "TaskUpdate", "TodoWrite"]
        ),
    }
    if model:
        kwargs["model"] = model
    if max_budget_usd is not None:
        kwargs["max_budget_usd"] = float(max_budget_usd)
    if max_turns is not None:
        kwargs["max_turns"] = int(max_turns)
    if session_id:
        kwargs["session_id"] = session_id
    if hooks:
        kwargs["hooks"] = hooks
    if env:
        kwargs["env"] = dict(env)
    if add_dirs:
        kwargs["add_dirs"] = [str(item) for item in add_dirs]
    if sandbox is not None:
        kwargs["sandbox"] = dict(sandbox)
    if extra_args:
        kwargs["extra_args"] = dict(extra_args)
    return ClaudeAgentOptions(**kwargs)


def sdk_transport_available() -> bool:
    """Return whether the compatible SDK protocol is available."""
    try:
        from agent_bridge_connect.permission_transport import (
            claude_sdk_protocol_capability,
        )

        return bool(claude_sdk_protocol_capability()["available"])
    except Exception:  # noqa: BLE001 - availability probes never raise.
        return False


def new_transport_run_id(task_id: str) -> str:
    return f"claude-{task_id or 'unknown'}-{uuid.uuid4().hex[:8]}"


def monotonic_deadline(timeout_s: float) -> float:
    return time.monotonic() + max(float(timeout_s), 0.1)


__all__ = [
    "ClaudeSDKControlTransport",
    "ClaudeSDKTransportError",
    "SDK_SESSION_MODE_UPDATE",
    "SDK_V2_BYPASS_MODE",
    "SDK_V2_RULE_UPDATE_TYPES",
    "SDK_V2_SESSION_DESTINATION",
    "build_sdk_options",
    "monotonic_deadline",
    "new_transport_run_id",
    "sdk_transport_available",
]
