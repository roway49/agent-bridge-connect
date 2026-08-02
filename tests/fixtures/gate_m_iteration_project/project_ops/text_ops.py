from __future__ import annotations


def normalize_label(value: str) -> str:
    words = value.strip().split()
    if not words:
        raise ValueError("label is required")
    return " ".join(words)


def title_case_label(value: str) -> str:
    return normalize_label(value).title()


def extract_keywords(value: str) -> list[str]:
    words = value.strip().split()
    seen: dict[str, None] = {}
    for w in words:
        key = w.strip().lower()
        if key and key not in seen:
            seen[key] = None
    return list(seen)


def make_slug(value: str) -> str:
    return normalize_label(value).lower().replace(" ", "-")
