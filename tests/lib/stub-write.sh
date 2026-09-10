#!/usr/bin/env bash
# tests/lib/stub-write.sh — write a stub file to a path without following a
# pre-existing symlink that might already be sitting at that path.
#
# A test fixture that symlinks a path (typically to point tests at a real
# file for reading, or to build a minimal stub PATH) and later writes a stub
# "over" that same path must not let that write follow the symlink. `>` and
# `cat >` both follow it silently — the write lands on whatever the symlink
# points at, with no error from the shell. That is exactly what corrupted
# the real scripts/lib/pr_intake_gate.py in a working tree during code-plane
# PR #103: a fixture created a symlink at a path, a later step in the same
# test wrote a stub to that path, and the write followed the link onto the
# real file.
#
# `set -C` / `noclobber` does NOT protect against this: noclobber only
# refuses `>` when the destination does not already exist. A symlink (to an
# existing file) already exists at the destination, so noclobber has nothing
# to object to — the write proceeds and follows the link.
#
# Usage:
#   stub_write <dest_path> <<'EOF'
#   ...stub file contents...
#   EOF
#
# Removes any existing file or symlink at <dest_path> first (rm -f, the
# idiom already used throughout this test suite for teardown), so the write
# that follows always creates a fresh regular file at <dest_path> rather
# than following a symlink to wherever it points.

stub_write() {
  local dest="${1:?stub_write: destination path required}"
  rm -f -- "$dest"
  cat > "$dest"
}
