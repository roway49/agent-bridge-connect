from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ExecutorLevel:
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


@dataclass
class ExecutorCapabilities:
    structured_output: bool = False
    streaming_events: bool = False
    resume: bool = False
    cancel: bool = False
    input_required: bool = False
    model_selection: bool = False
    multimodal: bool = False
    image_input: bool = False
    image_generation: bool = False
    image_editing: bool = False
    max_input_images: int | None = None
    parallelism: int = 1
    level: int = ExecutorLevel.L0


@dataclass
class ProbeResult:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartResult:
    ok: bool
    run_id: str = ""
    message: str = ""


@dataclass
class PollResult:
    status: str
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResult:
    ok: bool
    message: str = ""


@dataclass(frozen=True)
class SessionCleanupRequest:
    """Transient exact-session cleanup input; never persisted as the receipt."""

    executor: str
    session_id: str
    task_id: str = ""
    strategy: str = "none"
    project_path: str = ""


@dataclass(frozen=True)
class SessionCleanupCapability:
    capability: str
    strategy: str = "none"
    error_code: str = ""


@dataclass(frozen=True)
class SessionCleanupResult:
    state: str
    capability: str
    strategy: str
    error_code: str = ""
    retryable: bool = False
    next_attempt_at: str = ""


@dataclass
class DeliveryResult:
    ok: bool
    message: str = ""
    delivery_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ExecutorPort(ABC):
    @abstractmethod
    def probe(self) -> ProbeResult:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> ExecutorCapabilities:
        raise NotImplementedError

    @abstractmethod
    def start(self, task_packet: dict[str, Any]) -> StartResult:
        raise NotImplementedError

    @abstractmethod
    def poll(self, run_id: str) -> PollResult:
        raise NotImplementedError

    def cancel(self, run_id: str) -> AdapterResult:
        return AdapterResult(False, "cancel is not supported")

    def send_input(self, run_id: str, message: str) -> AdapterResult:
        return AdapterResult(False, "input is not supported")

    def session_cleanup_capability(
        self,
        request: SessionCleanupRequest,
    ) -> SessionCleanupCapability:
        return SessionCleanupCapability(
            capability="unsupported",
            strategy="none",
            error_code="session_cleanup_unsupported",
        )

    def cleanup_session(self, request: SessionCleanupRequest) -> SessionCleanupResult:
        """Fail closed until an Executor adapter implements an official delete action."""
        return SessionCleanupResult(
            state="unsupported",
            capability="unsupported",
            strategy="none",
            error_code="session_cleanup_unsupported",
            retryable=False,
        )


class NotifierPort(ABC):
    @abstractmethod
    def probe(self) -> ProbeResult:
        raise NotImplementedError

    @abstractmethod
    def send(self, notification: dict[str, Any]) -> DeliveryResult:
        raise NotImplementedError
