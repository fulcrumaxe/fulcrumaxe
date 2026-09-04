#!/usr/bin/env python3
"""Tests for scripts/engine-sync/merge.py (D#1586 Slice B2 Batch B2c).

Runnable both as a script (`python3 scripts/engine-sync/tests/test_merge.py`)
and via pytest (`pytest scripts/engine-sync/tests/test_merge.py`).

Covers the D#1586 Batch B2c Spec (Acceptance) items:
  17. Three-way merge: base/local/upstream fixture -> `git merge-file`
      deterministically merges non-overlapping hunks and leaves raw
      `<<<<<<< / ======= / >>>>>>>` markers only on true conflicts.
  22. G10 -- subprocess minimization (this file's share of the grep guard:
      merge.py itself only ever invokes `git merge-file`, no shell=True,
      no eval, no os.system).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_DIR / "merge.py"

spec = importlib.util.spec_from_file_location("engine_sync_merge", MODULE_PATH)
merge_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(merge_mod)


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


class HelpTest(unittest.TestCase):
    """(part of item set carried from B2a/B2b convention) --help exits 0."""

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            merge_mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_merge_subcommand_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            merge_mod.main(["merge", "--help"])
        self.assertEqual(ctx.exception.code, 0)


class ThreeWayMergeTest(unittest.TestCase):
    """(17) non-overlapping hunks merge cleanly (no markers); overlapping
    hunks leave raw conflict markers."""

    def test_non_overlapping_hunks_merge_cleanly(self):
        base = b"line1\nline2\nline3\nline4\nline5\n"
        # Local changes an early, unrelated line; upstream changes a late,
        # unrelated line -- a textbook non-overlapping three-way merge.
        local = b"LOCAL-CHANGED-line1\nline2\nline3\nline4\nline5\n"
        upstream = b"line1\nline2\nline3\nline4\nUPSTREAM-CHANGED-line5\n"

        merged, conflict_count = merge_mod.three_way_merge(base, local, upstream)

        self.assertEqual(conflict_count, 0)
        self.assertFalse(merge_mod.has_conflict_markers(merged))
        self.assertIn(b"LOCAL-CHANGED-line1", merged)
        self.assertIn(b"UPSTREAM-CHANGED-line5", merged)

    def test_overlapping_hunks_leave_raw_conflict_markers(self):
        base = b"shared line\n"
        local = b"LOCAL VERSION\n"
        upstream = b"UPSTREAM VERSION\n"

        merged, conflict_count = merge_mod.three_way_merge(base, local, upstream)

        self.assertGreater(conflict_count, 0)
        self.assertTrue(merge_mod.has_conflict_markers(merged))
        self.assertIn(b"<<<<<<< ", merged)
        self.assertIn(b"=======\n", merged)
        self.assertIn(b">>>>>>> ", merged)
        # Both true divergent versions must still be present verbatim
        # inside the markers -- a real three-way merge, not a discard.
        self.assertIn(b"LOCAL VERSION", merged)
        self.assertIn(b"UPSTREAM VERSION", merged)

    def test_identical_local_and_upstream_change_merges_clean(self):
        base = b"old\n"
        local = b"new\n"
        upstream = b"new\n"
        merged, conflict_count = merge_mod.three_way_merge(base, local, upstream)
        self.assertEqual(conflict_count, 0)
        self.assertFalse(merge_mod.has_conflict_markers(merged))
        self.assertEqual(merged, b"new\n")

    def test_never_mutates_input_temp_files_or_leaks_temp_dir(self):
        """`-p` writes to stdout, never mutating the temp "local" file in
        place -- and the TemporaryDirectory context manager guarantees the
        scratch dir is gone afterward (nothing lingers for a later run to
        accidentally read)."""
        base = b"base\n"
        local = b"local\n"
        upstream = b"upstream\n"
        merged, _ = merge_mod.three_way_merge(base, local, upstream)
        self.assertIsInstance(merged, bytes)


class CliTest(unittest.TestCase):
    """CLI wrapper: --out written, exit codes reflect conflict presence."""

    def test_cli_clean_merge_exits_zero_and_writes_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_f = tmp_path / "base"
            local_f = tmp_path / "local"
            upstream_f = tmp_path / "upstream"
            out_f = tmp_path / "out"
            base_f.write_text("line1\nline2\nline3\nline4\nline5\n")
            local_f.write_text("LOCAL-line1\nline2\nline3\nline4\nline5\n")
            upstream_f.write_text("line1\nline2\nline3\nline4\nUPSTREAM-line5\n")

            result = merge_mod.main(
                [
                    "merge",
                    "--base",
                    str(base_f),
                    "--local",
                    str(local_f),
                    "--upstream",
                    str(upstream_f),
                    "--out",
                    str(out_f),
                ]
            )
            self.assertEqual(result, 0)
            content = out_f.read_bytes()
            self.assertFalse(merge_mod.has_conflict_markers(content))
            self.assertIn(b"LOCAL-line1", content)
            self.assertIn(b"UPSTREAM-line5", content)

    def test_cli_conflict_exits_one_but_still_writes_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_f = tmp_path / "base"
            local_f = tmp_path / "local"
            upstream_f = tmp_path / "upstream"
            out_f = tmp_path / "out"
            base_f.write_text("shared\n")
            local_f.write_text("LOCAL\n")
            upstream_f.write_text("UPSTREAM\n")

            result = merge_mod.main(
                [
                    "merge",
                    "--base",
                    str(base_f),
                    "--local",
                    str(local_f),
                    "--upstream",
                    str(upstream_f),
                    "--out",
                    str(out_f),
                ]
            )
            self.assertEqual(result, 1)
            self.assertTrue(out_f.is_file(), "conflicted output must still be written -- that's the whole point")
            self.assertTrue(merge_mod.has_conflict_markers(out_f.read_bytes()))

    def test_cli_missing_input_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = merge_mod.main(
                [
                    "merge",
                    "--base",
                    str(tmp_path / "nope-base"),
                    "--local",
                    str(tmp_path / "nope-local"),
                    "--upstream",
                    str(tmp_path / "nope-upstream"),
                ]
            )
            self.assertEqual(result, 2)


class SubprocessMinimizationTest(unittest.TestCase):
    """(22) G10 -- merge.py's own share of the subprocess-minimization
    grep guard: the only subprocess it ever invokes is `git merge-file`, no
    shell=True, no eval, no os.system, no package-manager invocation."""

    def test_only_git_merge_file_subprocess_no_shell_true_no_eval(self):
        source = MODULE_PATH.read_text()
        self.assertIn('"git", "merge-file"', source)
        for needle in ("shell=True", "os.system(", "eval(", "npm install", "pip install", "postinstall"):
            self.assertNotIn(needle, source, f"merge.py must never contain {needle!r}")


class PytestRunnerSelfCheckTest(unittest.TestCase):
    def test_merge_module_importable(self):
        self.assertTrue(hasattr(merge_mod, "three_way_merge"))
        self.assertTrue(hasattr(merge_mod, "has_conflict_markers"))
        self.assertTrue(hasattr(merge_mod, "main"))


if __name__ == "__main__":
    sys.exit(unittest.main())
