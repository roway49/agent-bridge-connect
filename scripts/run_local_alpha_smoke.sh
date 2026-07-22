#!/bin/sh
set -eu

AGENTBC_BIN=${AGENTBC_BIN:-agentbc}
PYTHON_BIN=${PYTHON_BIN:-python3}
EVIDENCE_ROOT=${AGENTBC_SMOKE_ROOT:-"$(mktemp -d "${TMPDIR:-/tmp}/agentbc-alpha-smoke.XXXXXX")"}
BOARD="$EVIDENCE_ROOT/board"
WORKSPACE="$EVIDENCE_ROOT/managed-workspace"
PROJECT="$EVIDENCE_ROOT/customer-project"
CONFIG="$EVIDENCE_ROOT/config.toml"
STEPS="$EVIDENCE_ROOT/steps.yaml"
STATUS_JSON="$EVIDENCE_ROOT/status.json"

mkdir -p "$WORKSPACE" "$PROJECT"
printf 'board_root = "%s"\nworkspace_root = "%s"\n' "$BOARD" "$WORKSPACE" > "$CONFIG"
printf '%s\n' \
  'steps:' \
  '  - description: printf '\''AgentBC local Alpha smoke passed\n'\'' > agentbc-smoke.txt' \
  > "$STEPS"

AGENTBC_CONFIG_PATH="$CONFIG" "$AGENTBC_BIN" init --root "$BOARD" >/dev/null
CREATE_OUTPUT=$(AGENTBC_CONFIG_PATH="$CONFIG" "$AGENTBC_BIN" task create \
  --root "$BOARD" \
  --title "Local Alpha package smoke" \
  --assignee shell \
  --steps "$STEPS" \
  --customer-path "$PROJECT")
TASK_ID=$(printf '%s\n' "$CREATE_OUTPUT" | sed -n 's/^created: //p' | head -n 1)

if [ -z "$TASK_ID" ]; then
  echo "could not parse task id" >&2
  echo "$CREATE_OUTPUT" >&2
  exit 1
fi

AGENTBC_CONFIG_PATH="$CONFIG" "$AGENTBC_BIN" worker run \
  --root "$BOARD" \
  --executor shell \
  --once \
  --task-id "$TASK_ID"

AGENTBC_CONFIG_PATH="$CONFIG" "$AGENTBC_BIN" task status \
  "$TASK_ID" \
  --root "$BOARD" \
  --json > "$STATUS_JSON"

"$PYTHON_BIN" - "$STATUS_JSON" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
task = payload["current_task"]
if task["status"] != "completed":
    raise SystemExit(f"unexpected task status: {task['status']}")
report = pathlib.Path(task["workspace"]["report_file"])
if not report.is_file():
    raise SystemExit(f"report missing: {report}")
print(f"report: {report}")
PY

test -f "$PROJECT/agentbc-smoke.txt"
AGENTBC_CONFIG_PATH="$CONFIG" "$AGENTBC_BIN" task report "$TASK_ID" --root "$BOARD" >/dev/null

echo "smoke: passed"
echo "task: $TASK_ID"
echo "evidence: $EVIDENCE_ROOT"
