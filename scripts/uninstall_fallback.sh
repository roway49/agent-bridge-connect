#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
AgentBC fallback uninstaller for use when the agentbc CLI is unavailable.

Usage: uninstall_fallback.sh [data choices]
  --remove-records    Remove managed task records and reports.
  --keep-records      Preserve managed task records and reports.
  --remove-artifacts  Remove AgentBC default-workspace artifacts.
  --keep-artifacts    Preserve AgentBC default-workspace artifacts.
  -h, --help          Show this help.

Unspecified data choices are asked interactively and default to preservation.
Customer project paths are never cleanup targets.
EOF
}

REMOVE_RECORDS=
REMOVE_ARTIFACTS=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --remove-records) REMOVE_RECORDS=1 ;;
    --keep-records) REMOVE_RECORDS=0 ;;
    --remove-artifacts) REMOVE_ARTIFACTS=1 ;;
    --keep-artifacts) REMOVE_ARTIFACTS=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

ask_choice() {
  prompt=$1
  answer=
  if [ -t 0 ]; then
    printf '%s' "$prompt"
    IFS= read -r answer || {
      echo "uninstall choice required; pass explicit --remove/--keep flags" >&2
      return 2
    }
  elif answer=$( (
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r tty_answer < /dev/tty
    printf '%s' "$tty_answer"
  ) 2>/dev/null); then
    :
  elif IFS= read -r answer; then
    printf '%s' "$prompt" >&2
  else
    echo "uninstall choice required; run from a terminal or pass explicit --remove/--keep flags" >&2
    return 2
  fi
  case "$answer" in
    y|Y|yes|YES|Yes) printf '1\n' ;;
    *) printf '0\n' ;;
  esac
}

INSTALL_ROOT=${AGENTBC_ALPHA_HOME:-"$HOME/.agentbc-alpha"}
BIN_DIR=${AGENTBC_BIN_DIR:-"$HOME/.local/bin"}
CONFIG_PATH=${AGENTBC_CONFIG_PATH:-"$HOME/.abc/config.toml"}
DEFAULT_WORKSPACE_ROOT="$HOME/Documents/AgentBC/workspace"
DEFAULT_BOARD_ROOT="$DEFAULT_WORKSPACE_ROOT/record"
ACTIVE_HERMES_HOME=${HERMES_HOME:-"$HOME/.hermes"}
case "$ACTIVE_HERMES_HOME" in
  */profiles/*) HERMES_BASE=${ACTIVE_HERMES_HOME%%/profiles/*} ;;
  *) HERMES_BASE=$ACTIVE_HERMES_HOME ;;
esac
RUNNER_SPOOL=${AGENTBC_RUNNER_SPOOL:-"/tmp/agentbc-runner-v2-$(id -u)"}

config_value() {
  key=$1
  [ -f "$CONFIG_PATH" ] || return 0
  awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      value = $0
      sub(/^[^=]*=[[:space:]]*/, "", value)
      sub(/[[:space:]]*#.*/, "", value)
      sub(/^"/, "", value)
      sub(/"[[:space:]]*$/, "", value)
      print value
      exit
    }
  ' "$CONFIG_PATH"
}

BOARD_ROOT=${AGENTBC_BOARD_ROOT:-$(config_value board_root)}
WORKSPACE_ROOT=${AGENTBC_WORKSPACE_ROOT:-$(config_value workspace_root)}
BOARD_ROOT=${BOARD_ROOT:-$DEFAULT_BOARD_ROOT}
WORKSPACE_ROOT=${WORKSPACE_ROOT:-$DEFAULT_WORKSPACE_ROOT}
REPORT_ROOT="$WORKSPACE_ROOT/tasks/report"
ARTIFACT_ROOT="$WORKSPACE_ROOT/tasks/artifacts"
NORMALIZED_WORKSPACE_ROOT=${WORKSPACE_ROOT%/}

if [ -z "$REMOVE_RECORDS" ]; then
  REMOVE_RECORDS=$(ask_choice "Remove AgentBC runtime records at $BOARD_ROOT and reports at $REPORT_ROOT? [y/N] ")
fi
if [ -z "$REMOVE_ARTIFACTS" ]; then
  REMOVE_ARTIFACTS=$(ask_choice "Remove AgentBC default workspace artifacts at $ARTIFACT_ROOT? [y/N] ")
fi

remove_tree() {
  path=$1
  case "$path" in
    ""|"/"|"$HOME"|"."|"..")
      echo "refusing unsafe removal path: $path" >&2
      exit 1
      ;;
  esac
  if [ -e "$path" ] || [ -L "$path" ]; then
    rm -rf "$path"
    echo "removed: $path"
  fi
}

stop_runner_from_pidfile() {
  pidfile=$1
  [ -f "$pidfile" ] || return 0
  pid=$(awk 'NR == 1 { print $1 }' "$pidfile")
  case "$pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  command=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$command" in
    *agentbc*runner*|*agent_bridge_connect*runner*)
      kill -TERM "$pid" 2>/dev/null || true
      echo "stopped Runner: pid $pid"
      ;;
  esac
}

discover_runner_pids() {
  ps -axo pid=,command= 2>/dev/null | while read -r pid command; do
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    case "$command" in
      *agent_bridge_connect.cli*runner*serve*|*agentbc*runner*serve*) ;;
      *) continue ;;
    esac
    case "$command" in
      *"--spool $RUNNER_SPOOL"*|*"--spool=$RUNNER_SPOOL"*) printf '%s\n' "$pid" ;;
      *"--spool "*|*"--spool="*) ;;
      *)
        if [ "$RUNNER_SPOOL" = "/tmp/agentbc-runner-v2-$(id -u)" ]; then
          printf '%s\n' "$pid"
        fi
        ;;
    esac
  done
}

stop_all_runner_processes() {
  pids=$(discover_runner_pids | sort -u | tr '\n' ' ')
  [ -n "$pids" ] || return 0
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  attempts=0
  while [ "$attempts" -lt 50 ]; do
    alive=
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        alive="$alive $pid"
      fi
    done
    [ -z "$alive" ] && break
    attempts=$((attempts + 1))
    sleep 0.1
  done
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  sleep 0.1
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "unable to stop AgentBC Runner: pid $pid" >&2
      return 1
    fi
    echo "stopped Runner: pid $pid"
  done
}

if [ "${AGENTBC_UNINSTALL_SKIP_RUNNER:-0}" != "1" ]; then
  if command -v launchctl >/dev/null 2>&1; then
    launchctl remove com.agentbc.runner >/dev/null 2>&1 || true
  fi
  stop_runner_from_pidfile "$RUNNER_SPOOL/runner.pid"
  stop_runner_from_pidfile "$HOME/.abc/runner/runner.pid"
  stop_all_runner_processes
fi

if [ -L "$BIN_DIR/agentbc" ]; then
  target=$(readlink "$BIN_DIR/agentbc")
  case "$target" in
    "$INSTALL_ROOT"/*) remove_tree "$BIN_DIR/agentbc" ;;
    *) echo "preserved non-Alpha agentbc symlink: $BIN_DIR/agentbc" ;;
  esac
fi
if [ -f "$BIN_DIR/abc" ] && grep -q "# AgentBC-owned abc shim" "$BIN_DIR/abc"; then
  remove_tree "$BIN_DIR/abc"
fi

remove_tree "$HERMES_BASE/skills/agentbc"
if [ -d "$HERMES_BASE/profiles" ]; then
  for profile in "$HERMES_BASE"/profiles/*; do
    [ -d "$profile" ] || continue
    remove_tree "$profile/skills/agentbc"
  done
fi
if [ "$ACTIVE_HERMES_HOME" != "$HERMES_BASE" ]; then
  remove_tree "$ACTIVE_HERMES_HOME/skills/agentbc"
fi
remove_tree "$HOME/.claude/skills/agentbc"
remove_tree "$HOME/.codex/skills/agentbc"

remove_tree "$HOME/Library/LaunchAgents/com.agentbc.runner.plist"
if [ "${AGENTBC_UNINSTALL_SKIP_RUNNER:-0}" != "1" ]; then
  remove_tree "$RUNNER_SPOOL"
fi
if [ "$REMOVE_RECORDS" = "1" ]; then
  remove_tree "$BOARD_ROOT"
  remove_tree "$REPORT_ROOT"
else
  echo "preserved task records: $BOARD_ROOT"
  echo "preserved task reports: $REPORT_ROOT"
fi
if [ "$REMOVE_ARTIFACTS" = "1" ]; then
  remove_tree "$ARTIFACT_ROOT"
else
  echo "preserved default-workspace artifacts: $ARTIFACT_ROOT"
fi
if [ "$REMOVE_RECORDS" = "1" ] && [ "$REMOVE_ARTIFACTS" = "1" ] && \
   [ "$NORMALIZED_WORKSPACE_ROOT" = "$DEFAULT_WORKSPACE_ROOT" ]; then
  remove_tree "$HOME/Documents/AgentBC"
fi

remove_tree "$CONFIG_PATH"
CONFIG_ROOT=$(dirname "$CONFIG_PATH")
if [ "$(basename "$CONFIG_ROOT")" = ".abc" ]; then
  remove_tree "$CONFIG_ROOT"
fi
remove_tree "$INSTALL_ROOT"

TEMP_ROOT=${TMPDIR:-/tmp}
for trace in "$TEMP_ROOT"/agentbc-alpha-download.* "$TEMP_ROOT"/agentbc-alpha-smoke.*; do
  [ -e "$trace" ] || continue
  remove_tree "$trace"
done
if [ "$NORMALIZED_WORKSPACE_ROOT" = "$DEFAULT_WORKSPACE_ROOT" ]; then
  rmdir "$WORKSPACE_ROOT" 2>/dev/null || true
  rmdir "$HOME/Documents/AgentBC" 2>/dev/null || true
fi

echo "AgentBC fallback cleanup completed. Customer project paths were not touched."
