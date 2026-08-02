from __future__ import annotations

import argparse
import json
from typing import Any

from project_ops.math_ops import parse_numbers, summarize_numbers
from project_ops.text_ops import normalize_label


def build_payload(label: str, numbers_text: str) -> dict[str, Any]:
    numbers = parse_numbers(numbers_text)
    return {
        "label": normalize_label(label),
        "summary": summarize_numbers(numbers),
    }


def format_text_payload(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return (
        f'{payload["label"]}: '
        f'count={summary["count"]:g}, '
        f'sum={summary["sum"]:g}, '
        f'avg={summary["average"]:g}, '
        f'min={summary["minimum"]:g}, '
        f'max={summary["maximum"]:g}'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate M sample project CLI.")
    parser.add_argument("--label", required=True)
    parser.add_argument("--numbers", required=True, help="Comma-separated numbers.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    payload = build_payload(args.label, args.numbers)
    if args.format == "text":
        print(format_text_payload(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
