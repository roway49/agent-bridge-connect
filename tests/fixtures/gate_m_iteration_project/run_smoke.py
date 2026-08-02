from __future__ import annotations

from main import build_payload
from project_ops.text_ops import make_slug


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    payload = build_payload("  Agent   Bridge  ", "2,4,6")
    assert_equal(payload["label"], "Agent Bridge", "normalized label")
    summary = payload["summary"]
    assert_equal(summary["count"], 3.0, "count")
    assert_equal(summary["sum"], 12.0, "sum")
    assert_equal(summary["average"], 4.0, "average")
    assert_equal(summary["minimum"], 2.0, "minimum")
    assert_equal(summary["maximum"], 6.0, "maximum")
    assert_equal(make_slug("Agent Bridge"), "agent-bridge", "slug")
    print("gate_m_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
