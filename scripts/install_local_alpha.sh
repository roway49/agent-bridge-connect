#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /path/to/agentbc-*.whl" >&2
  exit 2
fi

WHEEL=$1
PYTHON_BIN=${PYTHON_BIN:-python3}
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

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_DIR/bin/python" -m pip install \
  --no-deps \
  --force-reinstall \
  "$WHEEL"

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
