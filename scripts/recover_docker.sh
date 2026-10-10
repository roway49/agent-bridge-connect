#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${PYTHON_BIN:-}" ]; then
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' 2>/dev/null; then
      PYTHON_BIN=$(command -v "$candidate")
      break
    fi
  done
fi
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' || {
  echo 'AgentBC requires Python 3.10 or newer. Set PYTHON_BIN to a supported interpreter.' >&2
  exit 2
}
set -- "$SCRIPT_DIR"/agentbc-*.whl "$@"
exec "$PYTHON_BIN" -c 'import sys; sys.path.insert(0,sys.argv.pop(1)); from agent_bridge_connect.docker_install_recovery import main; raise SystemExit(main())' "$@"
