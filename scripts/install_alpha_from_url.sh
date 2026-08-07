#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE_URL=${1:-${AGENTBC_ALPHA_BASE_URL:-}}
EXPECTED_SHA256=${AGENTBC_EXPECTED_SHA256:-}
RUN_SMOKE=${AGENTBC_RUN_SMOKE:-1}

if [ -z "$BASE_URL" ]; then
  echo "usage: curl -fsSL URL/install-agentbc-alpha.sh | sh -s -- URL" >&2
  exit 2
fi

BASE_URL=${BASE_URL%/}
VERSION=${AGENTBC_PRODUCT_VERSION:-}
if [ -z "$VERSION" ] && [ -f "$SCRIPT_DIR/build_provenance.py" ]; then
  VERSION=$(python3 "$SCRIPT_DIR/build_provenance.py" print-product-version --repo-root "$SCRIPT_DIR/..")
fi
if [ -z "$VERSION" ]; then
  VERSION=${BASE_URL##*/}
fi
case "$VERSION" in
  v[0-9]*.[0-9]*.[0-9]*[A-Z]*) ;;
  *)
    echo "could not determine AgentBC product version from release URL; set AGENTBC_PRODUCT_VERSION" >&2
    exit 2
    ;;
esac
BUNDLE_NAME="agentbc-$VERSION-macos-local-alpha"
ARCHIVE="$BUNDLE_NAME.tar.gz"
DOWNLOAD_ROOT=${AGENTBC_ALPHA_DOWNLOAD_ROOT:-"$(mktemp -d "${TMPDIR:-/tmp}/agentbc-alpha-download.XXXXXX")"}
mkdir -p "$DOWNLOAD_ROOT"

echo "downloading: $BASE_URL/$ARCHIVE"
curl -fL --retry 3 --retry-delay 1 -o "$DOWNLOAD_ROOT/$ARCHIVE" "$BASE_URL/$ARCHIVE"
curl -fL --retry 3 --retry-delay 1 -o "$DOWNLOAD_ROOT/$ARCHIVE.sha256" "$BASE_URL/$ARCHIVE.sha256"

MANIFEST_SHA256=$(awk 'NR == 1 { print $1 }' "$DOWNLOAD_ROOT/$ARCHIVE.sha256")
if [ -z "$MANIFEST_SHA256" ]; then
  echo "checksum manifest is empty" >&2
  exit 1
fi
if [ -n "$EXPECTED_SHA256" ] && [ "$EXPECTED_SHA256" != "$MANIFEST_SHA256" ]; then
  echo "checksum manifest does not match the checksum pinned by the serve command" >&2
  exit 1
fi

cd "$DOWNLOAD_ROOT"
shasum -a 256 -c "$ARCHIVE.sha256"
tar -xzf "$ARCHIVE"

BUNDLE_DIR="$DOWNLOAD_ROOT/$BUNDLE_NAME"
set -- "$BUNDLE_DIR"/*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "expected exactly one wheel in $BUNDLE_DIR" >&2
  exit 1
fi

"$BUNDLE_DIR/install_local_alpha.sh" "$1"

if [ "$RUN_SMOKE" = "1" ]; then
  AGENTBC_BIN=${AGENTBC_BIN:-"${AGENTBC_BIN_DIR:-$HOME/.local/bin}/agentbc"} \
    "$BUNDLE_DIR/run_local_alpha_smoke.sh"
fi

echo "download evidence: $DOWNLOAD_ROOT"
echo "next: export PATH=\"${AGENTBC_BIN_DIR:-$HOME/.local/bin}:\$PATH\""
echo "install: completed (setup included)"
