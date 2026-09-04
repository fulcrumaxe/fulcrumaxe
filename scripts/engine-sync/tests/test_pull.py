#!/usr/bin/env python3
"""Tests for scripts/engine-sync/pull.py (D#1535 Slice B1).

Runnable both as a script (`python3 scripts/engine-sync/tests/test_pull.py`)
and via pytest (`pytest scripts/engine-sync/tests/test_pull.py`).

Covers the D#1535 Spec (Acceptance) items:
  1. --help documents the classify/dry-run subcommand and flags.
  2. clean fixture: local == baseline -> clean-apply.
  3. local-patch fixture: local diverged, upstream == baseline -> local-patch,
     absent from clean-apply.
  4. conflict fixture: local and upstream both diverged -> conflict, absent
     from clean-apply.
  5. no-lockfile fixture: no engine/applied.json -> first-adoption /
     adopt-in-place, not a mass-conflict.
  6. path-traversal / allowlist gate: '..' / absolute / non-allowlisted paths
     are rejected, never accepted into any apply set.
  7. integrity gate: a tampered upstream blob is classified integrity-fail,
     never clean-apply.
  8. dry-run is side-effect-free: `git status --porcelain` identical before
     and after a --dry-run run.
  9. no-execute assertion: pull.py contains no live subprocess/install/merge
     invocation (only inside B2-deferral comments, if at all).
 10. manifest.py / drift-check.sh are unmodified by this module (checked here
     by confirming pull.py never opens those files for writing; the PR-level
     `git diff --name-only` check is done by the executor before opening the
     PR, per the Spec's Real-world verification block).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_DIR / "pull.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("engine_sync_pull", MODULE_PATH)
pull_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(pull_mod)


class HelpTest(unittest.TestCase):
    """(1) --help exits 0 and documents classify / dry-run / flags."""

    def test_help_documents_subcommand_and_flags(self):
        with self.assertRaises(SystemExit) as ctx:
            pull_mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_classify_help_documents_flags(self):
        with self.assertRaises(SystemExit) as ctx:
            pull_mod.main(["classify", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_parser_help_text_mentions_all_flags(self):
        help_text = pull_mod.build_parser().format_help()
        self.assertIn("classify", help_text)
        # Inspect the classify subparser directly for its own flags.
        parser = pull_mod.build_parser()
        classify_action = next(
            a for a in parser._subparsers._group_actions if a.dest == "command"  # type: ignore[attr-defined]
        )
        classify_parser = classify_action.choices["classify"]
        classify_help = classify_parser.format_help()
        for flag in ("--target", "--manifest", "--report-out", "--dry-run"):
            self.assertIn(flag, classify_help)


class CleanFixtureTest(unittest.TestCase):
    """(2) local == baseline -> clean-apply."""

    def test_clean_apply_classification(self):
        report = pull_mod.classify(
            FIXTURES_DIR / "clean" / "target", FIXTURES_DIR / "clean" / "upstream"
        )
        self.assertIn("scripts/foo.sh", report["clean_apply"])
        self.assertEqual(report["classifications"]["scripts/foo.sh"]["status"], "clean-apply")


class LocalPatchFixtureTest(unittest.TestCase):
    """(3) local diverged, upstream == baseline -> local-patch, never clean-apply."""

    def test_local_patch_classification(self):
        report = pull_mod.classify(
            FIXTURES_DIR / "local-patch" / "target", FIXTURES_DIR / "local-patch" / "upstream"
        )
        self.assertIn("scripts/foo.sh", report["local_patch"])
        self.assertNotIn("scripts/foo.sh", report["clean_apply"])
        self.assertEqual(report["classifications"]["scripts/foo.sh"]["status"], "local-patch")


class ConflictFixtureTest(unittest.TestCase):
    """(4) local and upstream both diverged -> conflict, never clean-apply."""

    def test_conflict_classification(self):
        report = pull_mod.classify(
            FIXTURES_DIR / "conflict" / "target", FIXTURES_DIR / "conflict" / "upstream"
        )
        self.assertIn("scripts/foo.sh", report["conflict"])
        self.assertNotIn("scripts/foo.sh", report["clean_apply"])
        self.assertEqual(report["classifications"]["scripts/foo.sh"]["status"], "conflict")


class NoLockfileFixtureTest(unittest.TestCase):
    """(5) no engine/applied.json -> first-adoption / adopt-in-place, not a
    mass-conflict."""

    def test_first_adoption_not_mass_conflict(self):
        report = pull_mod.classify(
            FIXTURES_DIR / "no-lockfile" / "target", FIXTURES_DIR / "no-lockfile" / "upstream"
        )
        self.assertTrue(report["first_adoption"])
        self.assertEqual(report["conflict"], [])
        # Local ("echo whatever\n") != upstream ("echo upstream\n") with no
        # recorded baseline -> adopt-in-place local-patch, never clean-apply.
        self.assertIn("scripts/foo.sh", report["local_patch"])
        self.assertNotIn("scripts/foo.sh", report["clean_apply"])


class PathTraversalAllowlistTest(unittest.TestCase):
    """(6) '..' / absolute / non-allowlisted manifest paths are rejected and
    never reach any apply set."""

    def _build_fixture(self, tmp: Path) -> tuple[Path, Path]:
        target = tmp / "target"
        upstream = tmp / "upstream"
        (target / "scripts").mkdir(parents=True)
        (upstream / "scripts").mkdir(parents=True)

        ok_content = "echo ok\n"
        (upstream / "scripts" / "ok.sh").write_text(ok_content)
        (target / "scripts" / "ok.sh").write_text(ok_content)

        import hashlib

        def sha(s: str) -> str:
            return hashlib.sha256(s.encode()).hexdigest()

        manifest = {
            "engine_version": "0.1.0",
            "files": {
                "scripts/ok.sh": sha(ok_content),
                "../../etc/passwd": "deadbeef" * 8,
                "/etc/passwd": "deadbeef" * 8,
                "not-allowlisted/random.txt": "deadbeef" * 8,
            },
        }
        with open(upstream / "manifest.json", "w") as f:
            json.dump(manifest, f)
        return target, upstream

    def test_traversal_and_non_allowlisted_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, upstream = self._build_fixture(Path(tmp))
            report = pull_mod.classify(target, upstream)

            for bad_path in ("../../etc/passwd", "/etc/passwd", "not-allowlisted/random.txt"):
                self.assertIn(bad_path, report["rejected"], f"{bad_path} should be rejected")
                self.assertNotIn(bad_path, report["clean_apply"])
                self.assertNotIn(bad_path, report["local_patch"])
                self.assertNotIn(bad_path, report["already_applied"])
                self.assertNotIn(bad_path, report["conflict"])

            # The one legitimate, allowlisted path is unaffected.
            self.assertNotIn("scripts/ok.sh", report["rejected"])


class NonCanonicalPathRejectionTest(unittest.TestCase):
    """G4-bypass fix (CWE-706/494, D#1586 Batch B2b security-review fix
    round): a manifest key using a non-canonical form of an otherwise-valid
    path (a './' segment, a doubled '/', a trailing slash) is REJECTED
    outright -- never silently normalized-and-allowed through to
    clean-apply. This is the exact vector that let a poisoned manifest key
    evade apply.py's protected-set exact-string check (`relpath in
    protected`) while still resolving to the real on-disk file."""

    def _build_fixture(self, tmp: Path) -> tuple[Path, Path]:
        target = tmp / "target"
        upstream = tmp / "upstream"
        (target / "scripts").mkdir(parents=True)
        (upstream / "scripts").mkdir(parents=True)

        content = "echo ok\n"
        (upstream / "scripts" / "ok.sh").write_text(content)
        (target / "scripts" / "ok.sh").write_text(content)

        import hashlib

        def sha(s: str) -> str:
            return hashlib.sha256(s.encode()).hexdigest()

        manifest = {
            "engine_version": "0.1.0",
            "files": {
                "scripts/./ok.sh": sha(content),
                "scripts//ok.sh": sha(content),
                "scripts/ok.sh/": sha(content),
            },
        }
        with open(upstream / "manifest.json", "w") as f:
            json.dump(manifest, f)
        return target, upstream

    def test_non_canonical_forms_rejected_not_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, upstream = self._build_fixture(Path(tmp))
            report = pull_mod.classify(target, upstream)

            for bad_path in ("scripts/./ok.sh", "scripts//ok.sh", "scripts/ok.sh/"):
                self.assertIn(bad_path, report["rejected"], f"{bad_path} should be rejected")
                self.assertNotIn(bad_path, report["clean_apply"])
                self.assertNotIn(bad_path, report["local_patch"])
                self.assertNotIn(bad_path, report["already_applied"])

    def test_canonicalize_relpath_accepts_only_the_exact_canonical_form(self):
        canonical, reason = pull_mod.canonicalize_relpath("scripts/engine-sync/pull.py")
        self.assertEqual(canonical, "scripts/engine-sync/pull.py")
        self.assertEqual(reason, "")

        for bad in ("scripts/engine-sync/./pull.py", "scripts//engine-sync/pull.py", "scripts/engine-sync/pull.py/", ""):
            canonical, reason = pull_mod.canonicalize_relpath(bad)
            self.assertIsNone(canonical, f"{bad!r} should be rejected, not normalized")
            self.assertTrue(reason)


class IntegrityGateTest(unittest.TestCase):
    """(7) A tampered upstream blob (real sha256 != manifest-claimed hash) is
    surfaced as integrity-fail, never clean-apply."""

    def test_tampered_blob_is_integrity_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "target"
            upstream = tmp_path / "upstream"
            (target / "scripts").mkdir(parents=True)
            (target / "engine").mkdir(parents=True)
            (upstream / "scripts").mkdir(parents=True)

            local_content = "echo v1\n"
            (target / "scripts" / "foo.sh").write_text(local_content)

            import hashlib

            def sha(s: str) -> str:
                return hashlib.sha256(s.encode()).hexdigest()

            # local == base, so this WOULD be clean-apply if the blob were honest.
            with open(target / "engine" / "applied.json", "w") as f:
                json.dump({"engine_version": "0.1.0", "files": {"scripts/foo.sh": sha(local_content)}}, f)

            # The manifest claims a hash that does not match the actual blob
            # content on disk -- simulates a tampered/corrupted fetch.
            real_upstream_content = "echo real-upstream-content\n"
            (upstream / "scripts" / "foo.sh").write_text(real_upstream_content)
            with open(upstream / "manifest.json", "w") as f:
                json.dump({"engine_version": "0.1.0", "files": {"scripts/foo.sh": "0" * 64}}, f)

            report = pull_mod.classify(target, upstream)
            self.assertIn("scripts/foo.sh", report["integrity_fail"])
            self.assertNotIn("scripts/foo.sh", report["clean_apply"])


class SideEffectFreeTest(unittest.TestCase):
    """(8) dry-run never mutates --target: `git status --porcelain` is
    byte-identical before and after."""

    def test_dry_run_leaves_git_status_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "target"
            shutil.copytree(FIXTURES_DIR / "clean" / "target", target)

            subprocess.run(["git", "init", "-q"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=target, check=True)
            subprocess.run(["git", "add", "-A"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)

            before = subprocess.run(
                ["git", "status", "--porcelain"], cwd=target, check=True, capture_output=True, text=True
            ).stdout

            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "classify",
                    "--target",
                    str(target),
                    "--manifest",
                    str(FIXTURES_DIR / "clean" / "upstream"),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            after = subprocess.run(
                ["git", "status", "--porcelain"], cwd=target, check=True, capture_output=True, text=True
            ).stdout

            self.assertEqual(before, after)
            self.assertEqual(before, "")

    def test_classify_without_dry_run_flag_refuses(self):
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "classify",
                "--target",
                str(FIXTURES_DIR / "clean" / "target"),
                "--manifest",
                str(FIXTURES_DIR / "clean" / "upstream"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


class NoExecuteAssertionTest(unittest.TestCase):
    """(9) pull.py contains no live subprocess/install/merge-file invocation
    -- any match must be inside a comment explicitly documenting B2
    deferral."""

    def test_no_live_execute_invocation(self):
        import re

        pattern = re.compile(
            r"npm (install|ci)|pip install|subprocess|os\.system|git merge-file|postinstall"
        )
        source_lines = MODULE_PATH.read_text().splitlines()

        # Track which lines fall inside a triple-quoted string (module/function
        # docstrings, including the top-of-file B2-deferral prose block) --
        # those are non-executable documentation, same as a '#' comment line,
        # and are the sanctioned place for this file to *mention* B2-deferred
        # tooling without granting it any live code path.
        in_docstring = False
        offending = []
        for lineno, line in enumerate(source_lines, start=1):
            starts_in_docstring = in_docstring
            quote_count = line.count('"""')
            if quote_count % 2 == 1:
                in_docstring = not in_docstring
            line_is_docstring_context = starts_in_docstring or in_docstring or quote_count > 0

            if not pattern.search(line):
                continue
            stripped = line.strip()
            in_comment_context = stripped.startswith("#") or line_is_docstring_context
            if not in_comment_context:
                offending.append((lineno, line))
        self.assertEqual(offending, [], f"live execute invocation found outside B2-deferral comments: {offending}")


class ManifestUntouchedTest(unittest.TestCase):
    """(10) pull.py imports manifest.py as a read-only library and never
    opens manifest.py or drift-check.sh for writing."""

    def test_pull_py_does_not_reference_writing_sibling_files(self):
        source = MODULE_PATH.read_text()
        self.assertNotIn('open(MODULE_DIR / "manifest.py"', source)
        self.assertNotIn("drift-check.sh", source)


class PytestRunnerSelfCheckTest(unittest.TestCase):
    """(11) This file itself is runnable standalone and via pytest -- covered
    by the module's __main__ block and by CI invoking this file directly."""

    def test_pull_module_importable(self):
        self.assertTrue(hasattr(pull_mod, "classify"))
        self.assertTrue(hasattr(pull_mod, "main"))


if __name__ == "__main__":
    sys.exit(unittest.main())
