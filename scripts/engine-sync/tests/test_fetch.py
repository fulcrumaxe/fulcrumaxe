#!/usr/bin/env python3
"""Tests for scripts/engine-sync/fetch.py (D#1586 Slice B2 Batch B2a).

Runnable both as a script (`python3 scripts/engine-sync/tests/test_fetch.py`)
and via pytest (`pytest scripts/engine-sync/tests/test_fetch.py`).

Covers the D#1586 Batch B2a Spec (Acceptance) items:
  1. --help exits 0.
  2. Signed-tag fetch: valid-signature fixture verifies end to end.
  3. G5 -- signature chain fails closed on (a) unsigned/tampered tag,
     (b) wrong-key signature, (c) per-file SHA-256 mismatch. Zero files
     written, non-zero exit, target git status untouched.
  4. G6 -- key pinning: a presented key whose fingerprint != pinned aborts.
  5. Base-blob retrieval: baseline + target both fetched in one verified
     fetch; base content available for a tracked file.
  6. Single transport: no per-file `gh api .../contents` call anywhere in
     the default path (grep-style regression test).
  7. This file is runnable under pytest (self-check, mirrors test_pull.py).

Each test builds its own throwaway GPG keyring (GNUPGHOME per test) and a
throwaway upstream git repo with signed tags -- no static fixtures on disk,
since GPG key material should never be committed even as a fixture.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MODULE_DIR / "fetch.py"

spec = importlib.util.spec_from_file_location("engine_sync_fetch", MODULE_PATH)
fetch_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(fetch_mod)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _gen_key(gnupg_home: Path, name: str) -> str:
    """Generate a passphrase-less ed25519 signing key in an isolated
    GNUPGHOME. Returns the 40-hex fingerprint."""
    gnupg_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    batch = gnupg_home / "genkey.batch"
    batch.write_text(
        "%no-protection\n"
        "Key-Type: EDDSA\n"
        "Key-Curve: ed25519\n"
        "Key-Usage: sign\n"
        f"Name-Real: {name}\n"
        f"Name-Email: {name.lower().replace(' ', '.')}@example.com\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    result = _run(["gpg", "--homedir", str(gnupg_home), "--batch", "--gen-key", str(batch)])
    assert result.returncode == 0, result.stderr
    listing = _run(["gpg", "--homedir", str(gnupg_home), "--batch", "--with-colons", "--list-secret-keys"])
    for line in listing.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    raise AssertionError(f"no fingerprint found after keygen: {listing.stdout}")


def _export_armored(gnupg_home: Path, fingerprint: str) -> str:
    result = _run(["gpg", "--homedir", str(gnupg_home), "--batch", "--armor", "--export", fingerprint])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "exported public key is empty"
    return result.stdout


def _write_trust(path: Path, fingerprint: str, armored_key: str) -> None:
    with open(path, "w") as f:
        json.dump({"pinned_fingerprint": fingerprint, "public_key_armored": armored_key}, f)


class FetchFixture:
    """Builds a throwaway upstream repo with one or more signed tags, each
    carrying its own engine/manifest.json + scripts/foo.sh, plus a trust.json
    pinned to a chosen signing key. Everything lives under a single
    TemporaryDirectory cleaned up on close()."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="engine-sync-fetch-test-")
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        (self.remote / "scripts").mkdir()
        (self.remote / "engine").mkdir()
        _run(["git", "init", "-q"], cwd=self.remote, check=True)
        _run(["git", "config", "user.email", "t@example.com"], cwd=self.remote, check=True)
        _run(["git", "config", "user.name", "t"], cwd=self.remote, check=True)

        self.signing_gnupg = self.root / "signing-gnupg"
        self.pinned_fpr = _gen_key(self.signing_gnupg, "Pinned Key")
        self.pinned_armored = _export_armored(self.signing_gnupg, self.pinned_fpr)

        self.other_gnupg = self.root / "other-gnupg"
        self.other_fpr = _gen_key(self.other_gnupg, "Other Key")

        self.trust_path = self.root / "trust.json"
        _write_trust(self.trust_path, self.pinned_fpr, self.pinned_armored)

        self.out_dir = self.root / "out"

    def close(self):
        self.tmp.cleanup()

    def _commit(self, content: str, version: str) -> None:
        (self.remote / "scripts" / "foo.sh").write_text(content)
        manifest = {"engine_version": version, "files": {"scripts/foo.sh": _sha256(content)}}
        with open(self.remote / "engine" / "manifest.json", "w") as f:
            json.dump(manifest, f)
        _run(["git", "add", "-A"], cwd=self.remote, check=True)
        _run(["git", "commit", "-q", "-m", version], cwd=self.remote, check=True)

    def commit_and_sign(self, content: str, version: str, tag: str, signing_home: Path, signing_fpr: str) -> None:
        self._commit(content, version)
        env = dict(**__import__("os").environ)
        env["GNUPGHOME"] = str(signing_home)
        result = _run(
            ["git", "-c", f"user.signingkey={signing_fpr}", "-c", "gpg.program=gpg", "tag", "-s", "-m", tag, tag],
            cwd=self.remote,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    def commit_and_tag_unsigned(self, content: str, version: str, tag: str) -> None:
        self._commit(content, version)
        result = _run(["git", "tag", "-a", "-m", tag, tag], cwd=self.remote)
        assert result.returncode == 0, result.stderr

    def commit_no_tag(self, content: str, version: str) -> None:
        self._commit(content, version)

    def git_status_snapshot(self) -> str:
        return _run(["git", "status", "--porcelain"], cwd=self.remote).stdout

    def run_fetch_cli(self, target_tag: str, baseline_tag: str | None = None) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(MODULE_PATH),
            "fetch",
            "--source",
            str(self.remote),
            "--target-tag",
            target_tag,
            "--trust-file",
            str(self.trust_path),
            "--out",
            str(self.out_dir),
        ]
        if baseline_tag:
            cmd += ["--baseline-tag", baseline_tag]
        return _run(cmd)


class HelpTest(unittest.TestCase):
    """(1) --help exits 0."""

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            fetch_mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_fetch_subcommand_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            fetch_mod.main(["fetch", "--help"])
        self.assertEqual(ctx.exception.code, 0)


class SignedTagFetchTest(unittest.TestCase):
    """(2) A valid-signature fixture verifies end to end: signature valid,
    tag resolves to SHA, blobs readable, exit 0."""

    def test_valid_signed_tag_verifies_and_materializes(self):
        fx = FetchFixture()
        try:
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.signing_gnupg, fx.pinned_fpr)
            result = fx.run_fetch_cli("v0.1.0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((fx.out_dir / "target" / "scripts" / "foo.sh").is_file())
            self.assertEqual((fx.out_dir / "target" / "scripts" / "foo.sh").read_text(), "echo v1\n")
            report = json.loads((fx.out_dir / "fetch-report.json").read_text())
            self.assertEqual(report["target"]["engine_version"], "0.1.0")
            self.assertEqual(report["target"]["signer_fingerprint"], fx.pinned_fpr)
        finally:
            fx.close()


class SignatureChainFailsClosedTest(unittest.TestCase):
    """(3) G5 -- three fixtures each abort with non-zero exit, zero files
    written, and leave the remote's git status byte-identical."""

    def test_a_unsigned_tag_aborts(self):
        fx = FetchFixture()
        try:
            fx.commit_and_tag_unsigned("echo v1\n", "0.1.0", "v0.1.0")
            before = fx.git_status_snapshot()
            result = fx.run_fetch_cli("v0.1.0")
            after = fx.git_status_snapshot()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(fx.out_dir.exists(), "out dir must not be created on verification failure")
            self.assertEqual(before, after)
        finally:
            fx.close()

    def test_b_wrong_key_signature_aborts(self):
        fx = FetchFixture()
        try:
            # Signed by "other" key, but trust.json pins "pinned" key.
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.other_gnupg, fx.other_fpr)
            before = fx.git_status_snapshot()
            result = fx.run_fetch_cli("v0.1.0")
            after = fx.git_status_snapshot()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verified fetch failed", result.stderr)
            self.assertFalse(fx.out_dir.exists())
            self.assertEqual(before, after)
        finally:
            fx.close()

    def test_c_blob_sha256_mismatch_aborts(self):
        fx = FetchFixture()
        try:
            content = "echo v1\n"
            fx._commit(content, "0.1.0")
            # Tamper the manifest AFTER computing the real content hash, so the
            # committed (and then signed) manifest claims a hash that does not
            # match the actual blob -- simulates a corrupted/tampered fetch
            # target whose tag signature is still otherwise valid.
            tampered_manifest = {"engine_version": "0.1.0", "files": {"scripts/foo.sh": "0" * 64}}
            with open(fx.remote / "engine" / "manifest.json", "w") as f:
                json.dump(tampered_manifest, f)
            _run(["git", "add", "-A"], cwd=fx.remote, check=True)
            _run(["git", "commit", "-q", "-m", "tamper"], cwd=fx.remote, check=True)
            env = dict(**__import__("os").environ)
            env["GNUPGHOME"] = str(fx.signing_gnupg)
            tag_result = _run(
                ["git", "-c", f"user.signingkey={fx.pinned_fpr}", "-c", "gpg.program=gpg", "tag", "-s", "-m", "v0.1.0", "v0.1.0"],
                cwd=fx.remote,
                env=env,
            )
            assert tag_result.returncode == 0, tag_result.stderr

            before = fx.git_status_snapshot()
            result = fx.run_fetch_cli("v0.1.0")
            after = fx.git_status_snapshot()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertFalse(fx.out_dir.exists())
            self.assertEqual(before, after)
        finally:
            fx.close()


class KeyPinningTest(unittest.TestCase):
    """(4) G6 -- a presented key whose fingerprint != pinned aborts (covered
    directly by the wrong-key-signature fixture above); additionally a
    trust.json whose pinned_fingerprint doesn't match its own armored key's
    real fingerprint is rejected outright (tampered trust file)."""

    def test_internally_inconsistent_trust_file_aborts(self):
        fx = FetchFixture()
        try:
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.signing_gnupg, fx.pinned_fpr)
            # Overwrite trust.json with a bogus pinned_fingerprint that does
            # not match the armored key's real fingerprint.
            bogus_fpr = "A" * 40
            _write_trust(fx.trust_path, bogus_fpr, fx.pinned_armored)
            result = fx.run_fetch_cli("v0.1.0")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(fx.out_dir.exists())
        finally:
            fx.close()

    def test_malformed_fingerprint_rejected(self):
        fx = FetchFixture()
        try:
            _write_trust(fx.trust_path, "not-a-fingerprint", fx.pinned_armored)
            result = fx.run_fetch_cli("v0.1.0")
            self.assertNotEqual(result.returncode, 0)
        finally:
            fx.close()


class BaseBlobRetrievalTest(unittest.TestCase):
    """(5) Base-blob retrieval: baseline (from applied.json.engine_version)
    and target both fetched+verified in one call; base content is retrievable
    for a tracked file."""

    def test_baseline_and_target_both_verified_and_available(self):
        fx = FetchFixture()
        try:
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.signing_gnupg, fx.pinned_fpr)
            fx.commit_and_sign("echo v2\n", "0.2.0", "v0.2.0", fx.signing_gnupg, fx.pinned_fpr)

            result = fx.run_fetch_cli("v0.2.0", baseline_tag="v0.1.0")
            self.assertEqual(result.returncode, 0, result.stderr)

            base_file = fx.out_dir / "base" / "scripts" / "foo.sh"
            target_file = fx.out_dir / "target" / "scripts" / "foo.sh"
            self.assertTrue(base_file.is_file())
            self.assertTrue(target_file.is_file())
            self.assertEqual(base_file.read_text(), "echo v1\n")
            self.assertEqual(target_file.read_text(), "echo v2\n")

            report = json.loads((fx.out_dir / "fetch-report.json").read_text())
            self.assertEqual(report["baseline"]["engine_version"], "0.1.0")
            self.assertEqual(report["target"]["engine_version"], "0.2.0")
        finally:
            fx.close()

    def test_baseline_derived_from_applied_json(self):
        fx = FetchFixture()
        try:
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.signing_gnupg, fx.pinned_fpr)
            fx.commit_and_sign("echo v2\n", "0.2.0", "v0.2.0", fx.signing_gnupg, fx.pinned_fpr)

            applied_json = fx.root / "applied.json"
            with open(applied_json, "w") as f:
                json.dump({"engine_version": "0.1.0", "files": {"scripts/foo.sh": _sha256("echo v1\n")}}, f)

            cmd = [
                sys.executable,
                str(MODULE_PATH),
                "fetch",
                "--source",
                str(fx.remote),
                "--target-tag",
                "v0.2.0",
                "--trust-file",
                str(fx.trust_path),
                "--out",
                str(fx.out_dir),
                "--applied-json",
                str(applied_json),
                "--tag-prefix",
                "v",
            ]
            result = _run(cmd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((fx.out_dir / "base" / "scripts" / "foo.sh").is_file())
        finally:
            fx.close()

    def test_no_applied_json_is_first_adoption_no_baseline_fetched(self):
        fx = FetchFixture()
        try:
            fx.commit_and_sign("echo v1\n", "0.1.0", "v0.1.0", fx.signing_gnupg, fx.pinned_fpr)
            result = fx.run_fetch_cli("v0.1.0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((fx.out_dir / "base").exists())
            report = json.loads((fx.out_dir / "fetch-report.json").read_text())
            self.assertIsNone(report["baseline"])
        finally:
            fx.close()


class ManifestPathCanonicalizationTest(unittest.TestCase):
    """Trust-boundary canonicalization (D#1586 Batch B2b security-review fix
    round, CWE-706/494): verify_manifest_blobs rejects a manifest key that
    is not already in canonical form (e.g. a './' segment), same gate as
    pull.py's validate_path -- a poisoned non-canonical key must never
    survive a verified fetch and reach apply.py's classify/G4 stage."""

    def test_non_canonical_manifest_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            extracted = Path(tmp) / "extracted"
            (extracted / "scripts").mkdir(parents=True)
            (extracted / "engine").mkdir()
            content = "echo hi\n"
            (extracted / "scripts" / "foo.sh").write_text(content)
            manifest = {
                "engine_version": "0.1.0",
                "files": {"scripts/./foo.sh": _sha256(content)},
            }
            with open(extracted / "engine" / "manifest.json", "w") as f:
                json.dump(manifest, f)

            with self.assertRaises(fetch_mod.FetchError) as ctx:
                fetch_mod.verify_manifest_blobs(extracted)
            self.assertIn("scripts/./foo.sh", str(ctx.exception))


class SingleTransportTest(unittest.TestCase):
    """(6) Resolves gap C: the default fetch path uses one verified git
    fetch / tree-at-SHA read, never a per-file `gh api .../contents` call."""

    def test_no_per_file_contents_call_in_source(self):
        source = MODULE_PATH.read_text()
        self.assertNotIn("gh api", source)
        self.assertNotIn("contents?ref=", source)
        self.assertNotIn("/contents/", source)

    def test_only_one_git_fetch_invocation_site(self):
        source = MODULE_PATH.read_text()
        # Exactly one call site constructs a `git fetch` subprocess command.
        self.assertEqual(source.count('"git", "fetch"'), 1)


class PytestRunnerSelfCheckTest(unittest.TestCase):
    """(7) This file itself is runnable standalone and via pytest."""

    def test_fetch_module_importable(self):
        self.assertTrue(hasattr(fetch_mod, "run_fetch"))
        self.assertTrue(hasattr(fetch_mod, "main"))


if __name__ == "__main__":
    sys.exit(unittest.main())
