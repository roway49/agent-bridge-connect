#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DIST_ROOT=${AGENTBC_ALPHA_DIST_ROOT:-"$REPOSITORY_ROOT/dist"}
PORT=${1:-8765}
VERSION=$(python3 "$SCRIPT_DIR/build_provenance.py" print-product-version --repo-root "$REPOSITORY_ROOT")
ARCHIVE="agentbc-$VERSION-macos-local-alpha.tar.gz"

if [ "${AGENTBC_ALPHA_SKIP_BUILD:-0}" != "1" ]; then
  "$SCRIPT_DIR/build_local_alpha_bundle.sh" "$DIST_ROOT"
elif [ ! -f "$DIST_ROOT/$ARCHIVE" ] || \
     [ ! -f "$DIST_ROOT/$ARCHIVE.sha256" ] || \
     [ ! -f "$DIST_ROOT/install-agentbc-alpha.sh" ] || \
     [ ! -f "$DIST_ROOT/uninstall-agentbc-alpha.sh" ]; then
  "$SCRIPT_DIR/build_local_alpha_bundle.sh" "$DIST_ROOT"
fi

PINNED_SHA256=$(awk 'NR == 1 { print $1 }' "$DIST_ROOT/$ARCHIVE.sha256")
if [ -z "$PINNED_SHA256" ]; then
  echo "could not read bundle checksum" >&2
  exit 1
fi

HOST=${AGENTBC_SERVE_HOST:-}
if [ -z "$HOST" ]; then
  for interface in en0 en1; do
    candidate=$(/usr/sbin/ipconfig getifaddr "$interface" 2>/dev/null || true)
    if [ -n "$candidate" ]; then
      HOST=$candidate
      break
    fi
  done
fi
HOST=${HOST:-127.0.0.1}
BASE_URL="http://$HOST:$PORT"

echo "AgentBC local Alpha server"
echo "directory: $DIST_ROOT"
echo "checksum: $PINNED_SHA256"
echo
echo "Run this on the test MacBook connected to the same network:"
echo "curl -fsSL $BASE_URL/install-agentbc-alpha.sh | AGENTBC_PRODUCT_VERSION=$VERSION AGENTBC_EXPECTED_SHA256=$PINNED_SHA256 sh -s -- $BASE_URL"
echo
echo "Reset the test MacBook to a clean pre-install state:"
echo "agentbc uninstall"
echo
echo "If the agentbc command is unavailable, use the standalone fallback:"
echo "curl -fsSL $BASE_URL/uninstall-agentbc-alpha.sh -o /tmp/uninstall-agentbc-alpha.sh && sh /tmp/uninstall-agentbc-alpha.sh"
echo
echo "Press Ctrl+C to stop the server."

cd "$DIST_ROOT"
exec python3 -m http.server "$PORT" --bind 0.0.0.0
