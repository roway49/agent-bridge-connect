#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /path/to/agentbc-*.whl" >&2
  exit 2
fi

WHEEL=$1
if [ -z "${PYTHON_BIN:-}" ]; then
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' 2>/dev/null; then
      PYTHON_BIN=$(command -v "$candidate")
      break
    fi
  done
fi
PYTHON_BIN=${PYTHON_BIN:-python3}
export PYTHON_BIN
INSTALL_ROOT=${AGENTBC_ALPHA_HOME:-"$HOME/.agentbc-alpha"}
BIN_DIR=${AGENTBC_BIN_DIR:-"$HOME/.local/bin"}
VENV_DIR="$INSTALL_ROOT/venv"
RUN_SETUP=${AGENTBC_RUN_SETUP:-1}
SETUP_NON_INTERACTIVE=${AGENTBC_SETUP_NONINTERACTIVE:-0}

if [ ! -f "$WHEEL" ]; then
  echo "wheel not found: $WHEEL" >&2
  exit 2
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "AgentBC requires Python 3.10 or newer." >&2
  exit 1
}

# Load the guard from the candidate wheel before changing the installed runtime.
exec "$PYTHON_BIN" -c 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from agent_bridge_connect.installation import native_script; raise SystemExit(native_script(Path(sys.argv[1]),Path(sys.argv[2]),[]))' "$WHEEL" "$0"
# AGENTBC_NATIVE_PAYLOAD
WHEEL=$1
PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_ROOT=${AGENTBC_ALPHA_HOME:-"$HOME/.agentbc-alpha"}
BIN_DIR=${AGENTBC_BIN_DIR:-"$HOME/.local/bin"}
VENV_DIR="$INSTALL_ROOT/venv"
RUN_SETUP=${AGENTBC_RUN_SETUP:-1}
SETUP_NON_INTERACTIVE=${AGENTBC_SETUP_NONINTERACTIVE:-0}
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Stop only this configuration's WebUI before replacing its Python files.
WEBUI_SUSPENDED=0
if "$VENV_DIR/bin/python" -c 'import agent_bridge_connect.web_service' 2>/dev/null; then
  "$VENV_DIR/bin/python" -m agent_bridge_connect.web_service --suspend-update
  WEBUI_SUSPENDED=1
fi
restore_webui() {
  if [ "$WEBUI_SUSPENDED" = 1 ]; then
    "$VENV_DIR/bin/python" -m agent_bridge_connect.web_service --resume-update || return 1
  fi
}
trap 'restore_webui' EXIT

PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_DIR/bin/python" -m pip install \
  --no-deps \
  --force-reinstall \
  "$WHEEL"
# PERM-104-002: Alpha/full installs select the probed Claude Agent SDK extra
# by default; AGENTBC_SKIP_CLAUDE_EXTRA=1 keeps a core-only install light.
if [ "${AGENTBC_SKIP_CLAUDE_EXTRA:-0}" != "1" ]; then
  PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_DIR/bin/python" -m pip install \
    "claude-agent-sdk==0.2.142" || {
      echo "warning: claude-agent-sdk==0.2.142 install failed; " \
           "the Claude SDK permission transport stays unsupported " \
           "(permission_transport_unsupported)" >&2
    }
fi

TARGET="$BIN_DIR/agentbc"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  echo "refusing to overwrite non-symlink: $TARGET" >&2
  exit 1
fi
ln -sfn "$VENV_DIR/bin/agentbc" "$TARGET"

echo "installed: $TARGET"
echo "version: $($TARGET --version 2>/dev/null || $VENV_DIR/bin/python -c 'import agent_bridge_connect; print(agent_bridge_connect.__version__)')"
echo "next: export PATH=\"$BIN_DIR:\$PATH\""
"$TARGET" --help >/dev/null

# Restore before interactive setup so a new user choice remains authoritative.
restore_webui
WEBUI_SUSPENDED=0

if [ "$RUN_SETUP" = "1" ]; then
  echo "setup: starting"
  if [ "$SETUP_NON_INTERACTIVE" = "1" ]; then
    "$TARGET" setup --non-interactive
  elif [ -t 1 ] && [ -r /dev/tty ]; then
    # curl | sh owns stdin, so setup must read confirmations from the terminal.
    "$TARGET" setup </dev/tty
  else
    echo "setup: no interactive terminal; using non-interactive mode"
    "$TARGET" setup --non-interactive
  fi
  echo "setup: completed"
else
  echo "setup: skipped (AGENTBC_RUN_SETUP=0)"
fi
