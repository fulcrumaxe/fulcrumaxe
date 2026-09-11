#!/usr/bin/env python3
"""Tests for detect_wrong_plane() and cmd_generate's wrong-plane refusal
(D#2510).

`manifest.py generate` used to hash whatever tree the script file happened
to sit in (`REPO_ROOT = Path(__file__).resolve().parents[2]`): run it in
place inside an executor's private-plane worktree and it silently wrote a
well-formed engine/manifest.json pinned to the wrong repo's contents --
exit 0, no error, no indication anything was wrong. A test that only
exercises the correct-tree path cannot see this defect (that is precisely
how it survived until now), so this file covers both directions: a tree
that looks like the private/engine plane must be refused, and the
correct-tree path must keep working, byte-identically.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "manifest.py"
spec = importlib.util.spec_from_file_location("engine_sync_manifest_plane_identity", MODULE_PATH)
manifest_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manifest_mod)


@contextlib.contextmanager
def _patched_root_and_manifest(root: Path, manifest_path: Path):
    """Point manifest_mod's REPO_ROOT/MANIFEST_PATH at a fixture tree for the
    duration of the block, then restore them. cmd_generate() reads both as
    bare module globals rather than parameters -- see test_manifest.py's
    _patched_manifest_paths, which does the same thing for cmd_verify()."""
    orig = (manifest_mod.REPO_ROOT, manifest_mod.MANIFEST_PATH)
    try:
        manifest_mod.REPO_ROOT = root
        manifest_mod.MANIFEST_PATH = manifest_path
        yield
    finally:
        manifest_mod.REPO_ROOT, manifest_mod.MANIFEST_PATH = orig


def _run_cmd_generate() -> tuple[int, str]:
    """Run cmd_generate() with stdout+stderr captured, return (exit_code, output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = manifest_mod.cmd_generate(argparse.Namespace())
    return rc, buf.getvalue()


class DetectWrongPlaneTest(unittest.TestCase):
    """Unit tests for the pure classifier -- no patching, no subprocess."""

    def test_populated_archive_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "archive"
            archive_dir.mkdir()
            (archive_dir / "some-old-tool-2026-01-01").mkdir()
            reason = manifest_mod.detect_wrong_plane(root)
            self.assertIsNotNone(reason)
            self.assertIn("archive/", reason)
            self.assertIn("git archive code-plane/main", reason)  # the remedy recipe

    def test_empty_archive_dir_is_not_refused(self):
        # An empty archive/ by itself is not proof of the wrong plane -- only
        # a *populated* one is the signal the Archive Protocol guarantees
        # (CLAUDE.md: 416 files on the private plane, 0 on the code plane).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "archive").mkdir()
            self.assertIsNone(manifest_mod.detect_wrong_plane(root))

    def test_absent_archive_dir_is_not_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(manifest_mod.detect_wrong_plane(root))

    def test_real_repo_tree_is_not_refused(self):
        # The code plane this manifest targets never carries a populated
        # archive/ -- scripts/ci/publish-denylist.sh denies every path
        # under it. Running this test from a real code-plane checkout must
        # find nothing to refuse.
        self.assertIsNone(manifest_mod.detect_wrong_plane(manifest_mod.REPO_ROOT))


class CmdGenerateWrongPlaneRefusalTest(unittest.TestCase):
    """The binding item (Spec item 2): generating against a tree that looks
    like the wrong plane is refused -- nothing is written, the exit code is
    non-zero, and the message names the cause and the remedy (Spec item 5)."""

    def test_generate_refuses_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "archive"
            archive_dir.mkdir()
            (archive_dir / "some-old-tool-2026-01-01").mkdir()
            manifest_path = root / "engine" / "manifest.json"

            with _patched_root_and_manifest(root, manifest_path):
                rc, output = _run_cmd_generate()

            self.assertEqual(rc, 2)
            self.assertIn("archive/", output)
            self.assertIn("git archive code-plane/main", output)
            self.assertFalse(manifest_path.exists(), "refusal must not write a manifest")

    def test_generate_still_works_on_a_correct_looking_tree(self):
        # Spec item 4: the correct-tree path must keep working. A synthetic
        # tree with no populated archive/ is not refused, and generate()
        # writes a manifest for whatever the real allowlist matches under it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "dummy-tool.sh").write_text("echo hi\n")
            (root / "engine").mkdir()
            (root / "engine" / "VERSION").write_text("0.0.0-test\n")
            manifest_path = root / "engine" / "manifest.json"

            with _patched_root_and_manifest(root, manifest_path):
                rc, output = _run_cmd_generate()

            self.assertEqual(rc, 0, output)
            self.assertTrue(manifest_path.exists())
            written = json.loads(manifest_path.read_text())
            self.assertIn("scripts/dummy-tool.sh", written["files"])


class CorrectTreeByteIdenticalTest(unittest.TestCase):
    """Spec item 4: on the real, unchanged code-plane tree, generate() must
    still produce output byte-identical to what it produces today -- the fix
    adds a refusal on the wrong-plane path, it must not touch the happy
    path's bytes. Runs the real cmd_generate() end-to-end (not a
    reimplementation of its internals) against a scratch manifest path so
    the committed engine/manifest.json is never overwritten by a test run."""

    def test_generate_on_the_real_tree_matches_the_committed_manifest(self):
        if not manifest_mod.MANIFEST_PATH.exists():
            self.skipTest("no committed engine/manifest.json to compare against")
        committed = manifest_mod.MANIFEST_PATH.read_text()
        real_root = manifest_mod.REPO_ROOT

        with tempfile.TemporaryDirectory() as tmp:
            scratch_manifest = Path(tmp) / "manifest.json"
            with _patched_root_and_manifest(real_root, scratch_manifest):
                rc, output = _run_cmd_generate()

            self.assertEqual(rc, 0, f"generate() must succeed on the real, correct-plane tree: {output}")
            self.assertEqual(
                scratch_manifest.read_text(),
                committed,
                "generate() must still be byte-identical to the committed manifest "
                "on an unchanged tree (Spec item 4)",
            )


if __name__ == "__main__":
    sys.exit(unittest.main())
