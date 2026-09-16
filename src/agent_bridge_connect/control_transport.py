"""Codex App Server stdio JSON-RPC transport.

The transport owns process I/O only. Approval protocol normalization and
durable control-plane state remain in their respective boundaries.
"""

from __future__ import annotations

import json
import select
import subprocess
import threading
from pathlib import Path
from typing import Any


class TransportClosed(RuntimeError):
    """The official stdio transport ended before the turn completed."""


class StdioJsonRpcTransport:
    """Minimal JSON-RPC stdio transport for Codex App Server."""

    def __init__(
        self,
        executable: str | Path,
        *,
        cwd: str | Path,
        command: list[str] | None = None,
    ) -> None:
        self.executable = str(executable)
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.command = list(command or [self.executable, "app-server", "--stdio"])
        self.process: subprocess.Popen[str] | None = None
        self._send_lock = threading.Lock()

    def start(self) -> None:
        if self.process is not None:
            return
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise TransportClosed(f"failed to start Codex App Server: {exc}") from exc

    def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise TransportClosed("Codex App Server transport is not started")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise TransportClosed("Codex App Server stdin closed") from exc

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise TransportClosed("Codex App Server transport is not started")
        stream = self.process.stdout
        if timeout_s is not None:
            try:
                ready, _, _ = select.select([stream], [], [], max(float(timeout_s), 0.0))
            except (OSError, ValueError) as exc:
                raise TransportClosed("Codex App Server stdout is unavailable") from exc
            if not ready:
                raise TimeoutError("Codex App Server receive timed out")
        line = stream.readline()
        if not line:
            raise TransportClosed("Codex App Server transport closed")
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            raise TransportClosed("Codex App Server sent invalid JSON") from exc
        if not isinstance(value, dict):
            raise TransportClosed("Codex App Server sent a non-object message")
        return value

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass


CodexAppServerTransport = StdioJsonRpcTransport

__all__ = ["CodexAppServerTransport", "StdioJsonRpcTransport", "TransportClosed"]
