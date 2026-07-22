from __future__ import annotations

import json
from pathlib import Path

from agent_bridge_connect.adapters import DeliveryResult
from agent_bridge_connect.reports import redact_secrets


class FileNotifier:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def send(self, notification: dict) -> DeliveryResult:
        """Append a redacted notification as one JSON line."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(redact_secrets(notification), ensure_ascii=False) + "\n"
                )
                handle.flush()
        except OSError as exc:
            return DeliveryResult(False, f"file notification failed: {exc}")
        return DeliveryResult(
            True,
            f"notification appended to {self.path}",
            str(self.path),
        )
