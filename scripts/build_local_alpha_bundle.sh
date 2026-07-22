#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DIST_ROOT_INPUT=${1:-"$REPOSITORY_ROOT/dist"}
mkdir -p "$DIST_ROOT_INPUT"
DIST_ROOT=$(CDPATH= cd -- "$DIST_ROOT_INPUT" && pwd)
VERSION=1.0.1A
BUNDLE_NAME="agentbc-$VERSION-macos-local-alpha"
BUILD_DIR="$DIST_ROOT/build-$VERSION"
BUNDLE_DIR="$DIST_ROOT/$BUNDLE_NAME"
ARCHIVE="$DIST_ROOT/$BUNDLE_NAME.tar.gz"
ARCHIVE_CHECKSUM="$ARCHIVE.sha256"
URL_INSTALLER="$DIST_ROOT/install-agentbc-alpha.sh"
URL_UNINSTALLER="$DIST_ROOT/uninstall-agentbc-alpha.sh"

rm -rf "$BUILD_DIR" "$BUNDLE_DIR" "$ARCHIVE" "$ARCHIVE_CHECKSUM" "$URL_INSTALLER" "$URL_UNINSTALLER"
mkdir -p "$BUILD_DIR" "$BUNDLE_DIR"

cd "$REPOSITORY_ROOT"
if [ -n "${AGENTBC_BUILD_PYTHON:-}" ]; then
  if ! "$AGENTBC_BUILD_PYTHON" -c 'import setuptools' >/dev/null 2>&1; then
    echo "setuptools missing from AGENTBC_BUILD_PYTHON: $AGENTBC_BUILD_PYTHON" >&2
    exit 1
  fi
  if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR=${UV_CACHE_DIR:-"$BUILD_DIR/.uv-cache"} \
      uv build --no-build-isolation --python "$AGENTBC_BUILD_PYTHON" --out-dir "$BUILD_DIR"
  elif "$AGENTBC_BUILD_PYTHON" -c 'import build.__main__' >/dev/null 2>&1; then
    "$AGENTBC_BUILD_PYTHON" -m build --no-isolation --outdir "$BUILD_DIR"
  else
    echo "build frontend missing: install uv or the Python build package" >&2
    exit 1
  fi
elif command -v uv >/dev/null 2>&1; then
  UV_CACHE_DIR=${UV_CACHE_DIR:-"$BUILD_DIR/.uv-cache"} \
    uv build --out-dir "$BUILD_DIR"
elif python3 -c 'import build' >/dev/null 2>&1; then
  python3 -m build --outdir "$BUILD_DIR"
else
  echo "build tool missing: install uv or the Python build package" >&2
  exit 1
fi

cp "$BUILD_DIR"/*.whl "$BUNDLE_DIR/"
cp "$BUILD_DIR"/*.tar.gz "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/install_local_alpha.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/install_alpha_from_url.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/run_local_alpha_smoke.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/uninstall_fallback.sh" "$BUNDLE_DIR/"
cp "$REPOSITORY_ROOT/docs/QUICK_START.md" "$BUNDLE_DIR/"
cp "$REPOSITORY_ROOT/docs/QUICK_START_ZH.md" "$BUNDLE_DIR/"

cd "$BUNDLE_DIR"
shasum -a 256 ./*.whl ./*.tar.gz ./install_local_alpha.sh ./install_alpha_from_url.sh ./run_local_alpha_smoke.sh ./uninstall_fallback.sh ./QUICK_START.md ./QUICK_START_ZH.md > SHA256SUMS
cd "$DIST_ROOT"
tar -czf "$ARCHIVE" "$BUNDLE_NAME"
shasum -a 256 "$BUNDLE_NAME.tar.gz" > "$BUNDLE_NAME.tar.gz.sha256"
cp "$SCRIPT_DIR/install_alpha_from_url.sh" "$URL_INSTALLER"
cp "$SCRIPT_DIR/uninstall_fallback.sh" "$URL_UNINSTALLER"

echo "bundle: $ARCHIVE"
echo "checksum: $ARCHIVE_CHECKSUM"
echo "curl installer: $URL_INSTALLER"
echo "fallback uninstaller: $URL_UNINSTALLER"
echo "directory: $BUNDLE_DIR"
