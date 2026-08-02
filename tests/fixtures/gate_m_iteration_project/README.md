# Gate M Iteration Project

This is a small existing project used to test parallel AgentBC iteration on a shared codebase.

## CLI Usage

### Smoke check
Run the full project smoke check:

```bash
python3 run_smoke.py
```

This exercises `build_payload` and `make_slug` with a fixed input and prints `gate_m_smoke: ok` on success.

### Direct CLI
Run the CLI directly:

```bash
python3 main.py --label "Agent Bridge" --numbers "2,4,6"
```

Arguments:

| Argument | Required | Description |
|---|---|---|
| `--label` | Yes | A text label. Whitespace is normalized (multiple spaces collapsed, trimmed). |
| `--numbers` | Yes | A comma-separated list of numbers. |

#### Output format

By default the CLI prints JSON to stdout:

```json
{
  "label": "Agent Bridge",
  "summary": {
    "average": 4.0,
    "count": 3.0,
    "maximum": 6.0,
    "median": 4.0,
    "minimum": 2.0,
    "range": 4.0,
    "sum": 12.0
  }
}
```

**Planned:** a `--format` flag will select between `json` (default) and `text`
(plain human-readable output).

### Output fields

The `summary` object includes: `count`, `sum`, `average`, `minimum`, `maximum`,
`median`, and `range`.

- `median` — the middle value of the sorted number list (average of two middle
  values for even-length lists).
- `range` — the difference between the maximum and minimum values.

### Text utilities

The text utilities module (`project_ops/text_ops.py`) exports the following:

| Function | Description |
|---|---|
| `normalize_label(value)` | Strips and collapses whitespace in a label. Raises `ValueError` on empty input. |
| `title_case_label(value)` | Returns the normalized label in Title Case. |
| `extract_keywords(value)` | Returns a deduplicated list of lowercased words from the label. |
| `make_slug(value)` | Returns a lowercased, hyphen-separated slug from the normalized label. |

## Parallel Edit Notes

This project is iterated on by four parallel tasks that each own a separate
file. To avoid merge conflicts:

| Task | Owned file(s) |
|---|---|
| Documentation | `README.md`, `CHANGELOG.md` |
| Main script | `main.py` |
| Math sub-script | `project_ops/math_ops.py` |
| Text sub-script | `project_ops/text_ops.py` |

Each task should edit **only** the files in its row. Cross-task changes are
communicated via this README (planned-field documentation) rather than by
editing another task's file.
