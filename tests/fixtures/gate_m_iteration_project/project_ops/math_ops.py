from __future__ import annotations


def parse_numbers(value: str) -> list[float]:
    numbers: list[float] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        numbers.append(float(item))
    if not numbers:
        raise ValueError("at least one number is required")
    return numbers


def summarize_numbers(numbers: list[float]) -> dict[str, float]:
    total = sum(numbers)
    sorted_nums = sorted(numbers)
    n = len(sorted_nums)
    if n % 2 == 1:
        median = sorted_nums[n // 2]
    else:
        median = (sorted_nums[n // 2 - 1] + sorted_nums[n // 2]) / 2.0
    return {
        "count": float(len(numbers)),
        "sum": total,
        "average": total / len(numbers),
        "minimum": min(numbers),
        "maximum": max(numbers),
        "median": median,
        "range": max(numbers) - min(numbers),
    }
