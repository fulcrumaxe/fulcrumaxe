#!/usr/bin/env python3
"""Tests for scripts/engine-sync/manifest.py (D#1528 Slice A, D#1928).

Runnable both as a script (`python3 scripts/engine-sync/tests/test_manifest.py`)
and via pytest (`pytest scripts/engine-sync/tests/test_manifest.py`).

Covers (per Spec item 7 of D#1528):
  (a) allowlist inclusion is exact -- an excluded pattern always wins even
      when a file also lives under an included directory.
  (b) tampering with one byte of a listed file makes `verify` fail and names
      that path.
  (c) `generate` is idempotent (repeated runs produce byte-identical output).

Also covers (D#1928 Spec items 6-7) -- `cmd_verify` itself, which nothing
here exercised before: it refuses to report clean about an empty or absent
`files` key, reports an `added` category for an allowlisted-but-unpinned
file, and prints the examined count on every exit path.
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
spec = importlib.util.spec_from_file_location("engine_sync_manifest", MODULE_PATH)
manifest_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manifest_mod)


@contextlib.contextmanager
def _patched_manifest_paths(root: Path, allowlist_path: Path, manifest_path: Path):
    """Point manifest_mod's module-level globals at a fixture tree for the
    duration of the block, then restore them.

    cmd_verify() reads REPO_ROOT / ALLOWLIST_PATH / MANIFEST_PATH as bare
    module globals rather than taking them as parameters (unlike
    collect_files()/read_allowlist(), which already accept a root/path).
    This is the fixture-friendliness the D#1928 Implementation Notes call
    out explicitly -- point them at a tmpdir rather than mutating the repo.
    """
    orig = (manifest_mod.REPO_ROOT, manifest_mod.ALLOWLIST_PATH, manifest_mod.MANIFEST_PATH)
    try:
        manifest_mod.REPO_ROOT = root
        manifest_mod.ALLOWLIST_PATH = allowlist_path
        manifest_mod.MANIFEST_PATH = manifest_path
        yield
    finally:
        manifest_mod.REPO_ROOT, manifest_mod.ALLOWLIST_PATH, manifest_mod.MANIFEST_PATH = orig


def _run_cmd_verify() -> tuple[int, str]:
    """Run cmd_verify() with stdout+stderr captured, return (exit_code, output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = manifest_mod.cmd_verify(argparse.Namespace())
    return rc, buf.getvalue()


class AllowlistExactnessTest(unittest.TestCase):
    """(a) A file matching an excluded pattern is never emitted even if it
    lives under an included dir."""

    def test_exclude_wins_over_include_same_file(self):
        includes = ["scripts/*.sh"]
        excludes = ["**/*secret*"]
        self.assertTrue(manifest_mod.is_included("scripts/secret_tool.sh", includes))
        self.assertTrue(manifest_mod.is_excluded("scripts/secret_tool.sh", excludes))

    def test_collect_files_excludes_despite_include_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "safe.sh").write_text("echo safe\n")
            (root / "scripts" / "token_holder.sh").write_text("echo secret-ish\n")

            includes = ["scripts/*.sh"]
            excludes = ["**/*token*"]
            files = manifest_mod.collect_files(root, includes, excludes)

            self.assertIn("scripts/safe.sh", files)
            self.assertNotIn("scripts/token_holder.sh", files)

    def test_real_allowlist_excludes_config_and_env_and_state(self):
        includes, excludes = manifest_mod.read_allowlist()
        # Even if a hypothetical include glob matched these, deny must win.
        self.assertTrue(manifest_mod.is_excluded("config.json", excludes))
        self.assertTrue(manifest_mod.is_excluded("nested/dir/config.json", excludes))
        self.assertTrue(manifest_mod.is_excluded(".env", excludes))
        self.assertTrue(manifest_mod.is_excluded(".autonomous-team/state.db", excludes))
        self.assertTrue(manifest_mod.is_excluded("scripts/lib/some_state_helper.sh", excludes))
        self.assertTrue(manifest_mod.is_excluded("backend/state_paths.py", excludes))


class TamperDetectionTest(unittest.TestCase):
    """(b) Tampering with one byte of a listed file makes `verify` fail and
    names that path."""

    def test_hash_changes_on_single_byte_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "sample.sh"
            f.write_text("echo hello\n")
            original_hash = manifest_mod.sha256_of(f)

            f.write_text("echo hellp\n")  # one byte changed: o -> p
            tampered_hash = manifest_mod.sha256_of(f)

            self.assertNotEqual(original_hash, tampered_hash)

    def test_collect_files_detects_tamper_via_recorded_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            target = root / "scripts" / "watched.sh"
            target.write_text("echo original\n")

            includes = ["scripts/*.sh"]
            excludes: list[str] = []
            recorded = manifest_mod.collect_files(root, includes, excludes)
            recorded_hash = recorded["scripts/watched.sh"]

            target.write_text("echo tampered\n")
            recomputed = manifest_mod.collect_files(root, includes, excludes)
            recomputed_hash = recomputed["scripts/watched.sh"]

            self.assertNotEqual(recorded_hash, recomputed_hash)


class IdempotencyTest(unittest.TestCase):
    """(c) generate is idempotent -- repeated collect_files() runs over an
    unchanged tree produce identical output."""

    def test_collect_files_repeated_runs_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "a.sh").write_text("echo a\n")
            (root / "scripts" / "b.sh").write_text("echo b\n")

            includes = ["scripts/*.sh"]
            excludes: list[str] = []

            first = manifest_mod.collect_files(root, includes, excludes)
            second = manifest_mod.collect_files(root, includes, excludes)

            self.assertEqual(first, second)

    def test_real_generate_is_byte_identical_across_runs(self):
        """End-to-end idempotency against the real repo tree, without
        mutating the committed engine/manifest.json."""
        includes, excludes = manifest_mod.read_allowlist()
        first = manifest_mod.collect_files(manifest_mod.REPO_ROOT, includes, excludes)
        second = manifest_mod.collect_files(manifest_mod.REPO_ROOT, includes, excludes)
        self.assertEqual(first, second)


class CmdVerifyEmptyManifestRefusalTest(unittest.TestCase):
    """D#1928 Spec item 6: `cmd_verify` refuses to be clean about nothing.
    A manifest with `"files": {}`, and one with no `files` key, each exit
    non-zero and name the cause -- neither is a 0-file clean pass."""

    def test_empty_files_dict_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"engine_version": "0.0.0", "files": {}}))
            allowlist_path = root / "allowlist.txt"  # unused on this path

            with _patched_manifest_paths(root, allowlist_path, manifest_path):
                rc, output = _run_cmd_verify()

            self.assertEqual(rc, 2)
            self.assertIn("'files' is empty", output)

    def test_missing_files_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"engine_version": "0.0.0"}))
            allowlist_path = root / "allowlist.txt"  # unused on this path

            with _patched_manifest_paths(root, allowlist_path, manifest_path):
                rc, output = _run_cmd_verify()

            self.assertEqual(rc, 2)
            self.assertIn("no 'files' key", output)


class CmdVerifyAddedCategoryTest(unittest.TestCase):
    """D#1928 Spec item 2/deliverable: an allowlisted file with no pin is
    reportable as `added`, distinct from `drifted`/`missing`. Before this,
    `cmd_verify` only ever iterated manifest["files"], so a candidate with
    no pin was invisible to it, not merely unreported."""

    def _fixture(self, tmp: str) -> tuple[Path, Path, Path]:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "pinned.sh").write_text("echo pinned\n")
        (root / "scripts" / "unpinned.sh").write_text("echo new\n")

        allowlist_path = root / "allowlist.txt"
        allowlist_path.write_text("[include]\nscripts/*.sh\n\n[exclude]\n")

        pinned_hash = manifest_mod.sha256_of(root / "scripts" / "pinned.sh")
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps({"engine_version": "0.0.0", "files": {"scripts/pinned.sh": pinned_hash}})
        )
        return root, allowlist_path, manifest_path

    def test_unpinned_allowlisted_file_reported_as_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, allowlist_path, manifest_path = self._fixture(tmp)
            with _patched_manifest_paths(root, allowlist_path, manifest_path):
                rc, output = _run_cmd_verify()

            self.assertEqual(rc, 1)
            self.assertIn("ADDED", output)
            self.assertIn("scripts/unpinned.sh", output)
            # The already-pinned, unchanged file must NOT be reported as
            # drifted or missing just because something else was added.
            self.assertNotIn("changed: scripts/pinned.sh", output)
            self.assertNotIn("missing: scripts/pinned.sh", output)

    def test_fully_pinned_tree_reports_no_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, allowlist_path, manifest_path = self._fixture(tmp)
            # Pin the previously-unpinned file too, so nothing is added.
            pinned_hash = manifest_mod.sha256_of(root / "scripts" / "pinned.sh")
            unpinned_hash = manifest_mod.sha256_of(root / "scripts" / "unpinned.sh")
            manifest_path.write_text(
                json.dumps(
                    {
                        "engine_version": "0.0.0",
                        "files": {
                            "scripts/pinned.sh": pinned_hash,
                            "scripts/unpinned.sh": unpinned_hash,
                        },
                    }
                )
            )
            with _patched_manifest_paths(root, allowlist_path, manifest_path):
                rc, output = _run_cmd_verify()

            self.assertEqual(rc, 0)
            self.assertIn("clean", output)


class CmdVerifyExaminedCountTest(unittest.TestCase):
    """D#1928 Spec item 7: verify prints the number of entries examined on
    every exit path, including the clean one."""

    def test_clean_path_prints_examined_count_on_real_repo(self):
        rc, output = _run_cmd_verify()
        with open(manifest_mod.MANIFEST_PATH) as f:
            pinned_count = len(json.load(f)["files"])
        self.assertEqual(rc, 0, f"expected the real repo to verify clean; got: {output}")
        self.assertIn(str(pinned_count), output)
        self.assertGreater(pinned_count, 0)

    def test_drift_path_prints_examined_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            target = root / "scripts" / "watched.sh"
            target.write_text("echo original\n")
            allowlist_path = root / "allowlist.txt"
            allowlist_path.write_text("[include]\nscripts/*.sh\n\n[exclude]\n")
            original_hash = manifest_mod.sha256_of(target)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"engine_version": "0.0.0", "files": {"scripts/watched.sh": original_hash}})
            )
            target.write_text("echo tampered\n")  # now drifts from the pin

            with _patched_manifest_paths(root, allowlist_path, manifest_path):
                rc, output = _run_cmd_verify()

            self.assertEqual(rc, 1)
            self.assertIn("examined 1 pinned entries", output)


class ManifestShapeTest(unittest.TestCase):
    def test_no_denied_path_in_real_allowlist_collection(self):
        includes, excludes = manifest_mod.read_allowlist()
        files = manifest_mod.collect_files(manifest_mod.REPO_ROOT, includes, excludes)
        denied = [f for f in files if manifest_mod.is_excluded(f, excludes)]
        self.assertEqual(denied, [])


if __name__ == "__main__":
    sys.exit(unittest.main())
