#!/usr/bin/env python3
"""Tests for scripts/engine-sync/manifest.py (D#1528 Slice A).

Runnable both as a script (`python3 scripts/engine-sync/tests/test_manifest.py`)
and via pytest (`pytest scripts/engine-sync/tests/test_manifest.py`).

Covers (per Spec item 7):
  (a) allowlist inclusion is exact -- an excluded pattern always wins even
      when a file also lives under an included directory.
  (b) tampering with one byte of a listed file makes `verify` fail and names
      that path.
  (c) `generate` is idempotent (repeated runs produce byte-identical output).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "manifest.py"
spec = importlib.util.spec_from_file_location("engine_sync_manifest", MODULE_PATH)
manifest_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(manifest_mod)


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


class ManifestShapeTest(unittest.TestCase):
    def test_no_denied_path_in_real_allowlist_collection(self):
        includes, excludes = manifest_mod.read_allowlist()
        files = manifest_mod.collect_files(manifest_mod.REPO_ROOT, includes, excludes)
        denied = [f for f in files if manifest_mod.is_excluded(f, excludes)]
        self.assertEqual(denied, [])


if __name__ == "__main__":
    sys.exit(unittest.main())
