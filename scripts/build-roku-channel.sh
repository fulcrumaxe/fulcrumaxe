#!/usr/bin/env bash
# build-roku-channel.sh — package the Roku channel with a specific API base URL.
#
# Usage:
#   bash scripts/build-roku-channel.sh <API_BASE_URL>
#
# Example:
#   bash scripts/build-roku-channel.sh https://lafk-api.myaccount.workers.dev
#
# Output: dist/roku-channel.zip (manifest at the archive root, as Roku requires)
# The source tree is never modified — all patching happens in a temp directory.

set -euo pipefail

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <API_BASE_URL>" >&2
  echo "  e.g. $0 https://lafk-api.myaccount.workers.dev" >&2
  exit 1
fi

API_BASE_URL="$1"

# Strip trailing slash so we don't double-slash endpoint paths in the channel.
API_BASE_URL="${API_BASE_URL%/}"

# ---------------------------------------------------------------------------
# Locate repo root and source channel directory
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROKU_SRC="$REPO_ROOT/apps/roku"
CONTENT_TASK="$ROKU_SRC/components/ContentTask.brs"

if [[ ! -d "$ROKU_SRC" ]]; then
  echo "ERROR: apps/roku directory not found at $ROKU_SRC" >&2
  exit 1
fi

if [[ ! -f "$CONTENT_TASK" ]]; then
  echo "ERROR: ContentTask.brs not found at $CONTENT_TASK" >&2
  exit 1
fi

# Verify the constant we're going to patch actually exists in the file.
if ! grep -q 'DEFAULT_API_BASE' "$CONTENT_TASK"; then
  echo "ERROR: DEFAULT_API_BASE constant not found in $CONTENT_TASK — cannot patch." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Set up output directory
# ---------------------------------------------------------------------------
OUT_DIR="$REPO_ROOT/dist"
OUT_ZIP="$OUT_DIR/roku-channel.zip"
mkdir -p "$OUT_DIR"

# ---------------------------------------------------------------------------
# Copy channel to a temp directory so we never touch the source tree
# ---------------------------------------------------------------------------
TMPDIR_BASE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT

CHANNEL_DIR="$TMPDIR_BASE/roku-channel"
cp -r "$ROKU_SRC/." "$CHANNEL_DIR"

# ---------------------------------------------------------------------------
# Patch DEFAULT_API_BASE in the copy of ContentTask.brs
# ---------------------------------------------------------------------------
PATCHED_TASK="$CHANNEL_DIR/components/ContentTask.brs"

# The line looks like:   DEFAULT_API_BASE = "https://lafk-api.workers.dev"
# sed replaces the value between the double-quotes on that specific line.
sed -i "s|DEFAULT_API_BASE = \"[^\"]*\"|DEFAULT_API_BASE = \"$API_BASE_URL\"|g" "$PATCHED_TASK"

# Confirm the patch landed.
if ! grep -q "DEFAULT_API_BASE = \"$API_BASE_URL\"" "$PATCHED_TASK"; then
  echo "ERROR: sed patch did not apply — check ContentTask.brs format." >&2
  exit 1
fi

echo "Patched DEFAULT_API_BASE → $API_BASE_URL"

# ---------------------------------------------------------------------------
# Build the zip with manifest at the archive root
# ---------------------------------------------------------------------------
# Roku requires the manifest file to be at the top level of the zip.
# We zip from inside the channel directory so there is no wrapping folder.
rm -f "$OUT_ZIP"

if command -v zip >/dev/null 2>&1; then
  (
    cd "$CHANNEL_DIR"
    zip -r "$OUT_ZIP" . \
      --exclude "*.DS_Store" \
      --exclude "bsconfig.json" \
      --exclude "*.lock" \
      --exclude "node_modules/*" \
      --exclude "README.md" \
      --exclude "source/schema/*"
  )
else
  # Fall back to Python's zipfile module (always available with Python 3).
  python3 - "$CHANNEL_DIR" "$OUT_ZIP" <<'PYEOF'
import sys, os, zipfile, fnmatch

src_dir = sys.argv[1]
out_zip = sys.argv[2]

EXCLUDES = ["*.DS_Store", "bsconfig.json", "*.lock"]

def is_excluded(rel_path):
    rel_norm = rel_path.replace("\\", "/")
    parts = rel_norm.split("/")
    # Exclude README.md at the channel root
    if rel_norm == "README.md":
        return True
    # Exclude the entire source/schema/ directory
    if rel_norm.startswith("source/schema/"):
        return True
    for part in parts:
        for pat in EXCLUDES:
            if fnmatch.fnmatch(part, pat):
                return True
        if part == "node_modules":
            return True
    return False

with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src_dir):
        # Skip node_modules subtrees early
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for fname in files:
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, src_dir)
            if not is_excluded(rel):
                zf.write(full, rel)
PYEOF
fi

# ---------------------------------------------------------------------------
# Verify manifest is at the root of the archive
# ---------------------------------------------------------------------------
if ! unzip -l "$OUT_ZIP" | grep -q '^\s*[0-9]*\s*[0-9-]*\s*[0-9:]*\s*manifest$'; then
  echo "WARNING: manifest not found at archive root — double-check zip layout." >&2
fi

echo ""
echo "Built: $OUT_ZIP"
echo "Next:  sideload at http://<roku-ip>  (username rokudev, password you set in dev mode)"
