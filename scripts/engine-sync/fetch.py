#!/usr/bin/env python3
"""engine-sync verified fetch — Slice B2 Batch B2a of D#1586 (follow-on to
Slice B1 / D#1535, which explicitly deferred this).

Deterministic, zero-LLM, zero-writes-to-sibling-tree trust anchor for the
engine-sync update-distribution channel. Given a signed release tag in the
upstream engine repo (fulcrumaxe/fulcrumaxe), this tool
fetches and verifies it end-to-end:

  signed tag -> pinned key (TOFU, engine/trust.json) -> commit SHA ->
  per-file SHA-256 (engine/manifest.json) -> verified local blob mirror

...and, given a sibling's currently-applied engine_version, ALSO fetches and
verifies the baseline tag in the same single `git fetch` call, so the
common-ancestor blob content needed for a real three-way merge (Slice B2b/c)
is available without a second network round trip (resolves the
technical-architect's "gap A").

It NEVER writes to a sibling's working tree (there is no --target argument
here at all -- that concept belongs to apply.py / Batch B2b). The only
filesystem writes this tool ever performs are: a private temp scratch dir
(cleaned up unconditionally) and, ONLY on full success of every verification
step, the caller-supplied --out mirror directory. On ANY verification
failure -- unsigned/tampered tag, wrong-key signature, key-fingerprint
mismatch, or a per-file SHA-256 mismatch -- this tool aborts with a non-zero
exit code and --out is left completely untouched (never created, never
partially populated). Fail-closed, always.

Transport: a single `git fetch` invocation (both target and baseline
refspecs together, when a baseline is requested) followed by a local
`git archive | tar -x` per verified tag. This is the sole transport path --
there is no per-file GitHub contents-API read anywhere in this module (see
the no-per-file-contents regression test).

Subcommands:
  fetch   Verify + materialize a target tag (and optionally a baseline tag)
          into --out. This is the only subcommand Slice B2a defines.

engine/trust.json shape (committed in the SIBLING repo, TOFU-pinned once by
a human; rotation is a human-gated runbook step -- see wiki, never silent
re-TOFU in code):
  {
    "pinned_fingerprint": "<40-hex uppercase v4 GPG fingerprint>",
    "public_key_armored": "-----BEGIN PGP PUBLIC KEY BLOCK-----\\n...\\n"
  }
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
import manifest as manifest_mod  # noqa: E402  (read_allowlist, is_included, is_excluded)
import pull as pull_mod  # noqa: E402  (canonicalize_relpath -- same trust-boundary gate as pull.py's validate_path)

VALIDSIG_RE = re.compile(r"^\[GNUPG:\] VALIDSIG ([0-9A-F]{40}) ")
ENGINE_MANIFEST_RELPATH = "engine/manifest.json"


class FetchError(Exception):
    """Any verification failure. Caller must treat this as fail-closed:
    non-zero exit, --out untouched."""


def load_trust(trust_path: Path) -> dict:
    with open(trust_path) as f:
        trust = json.load(f)
    if "pinned_fingerprint" not in trust or "public_key_armored" not in trust:
        raise FetchError(f"malformed trust file (missing keys): {trust_path}")
    fpr = trust["pinned_fingerprint"].strip().upper()
    if not re.fullmatch(r"[0-9A-F]{40}", fpr):
        raise FetchError(f"pinned_fingerprint is not a 40-hex v4 fingerprint: {fpr!r}")
    trust["pinned_fingerprint"] = fpr
    return trust


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def import_pinned_key(gnupg_home: Path, armored_key: str, pinned_fingerprint: str) -> None:
    """Import ONLY the pinned public key into an isolated, single-purpose
    GNUPGHOME. Defense in depth: verify the imported key's own fingerprint
    matches trust.json's claimed pinned_fingerprint -- an internally
    inconsistent (tampered) trust.json must fail closed here, before any
    tag is ever checked against it."""
    result = _run(
        ["gpg", "--homedir", str(gnupg_home), "--batch", "--import"],
        input=armored_key,
    )
    if result.returncode != 0:
        raise FetchError(f"failed to import pinned public key: {result.stderr.strip()}")

    listing = _run(
        ["gpg", "--homedir", str(gnupg_home), "--batch", "--with-colons", "--list-keys"]
    )
    fingerprints = [
        line.split(":")[9] for line in listing.stdout.splitlines() if line.startswith("fpr:")
    ]
    if pinned_fingerprint not in fingerprints:
        raise FetchError(
            "trust.json is internally inconsistent: pinned_fingerprint does not match "
            f"the fingerprint of public_key_armored (imported: {fingerprints})"
        )


def git_fetch_tags(scratch_repo: Path, source: str, tags: list[str]) -> None:
    """Single verified transport call: one `git fetch` for every requested
    tag (target + baseline together, when both are requested). This is the
    ONLY network/transport operation this module performs -- resolves gap C
    (no per-file GitHub contents-API read anywhere on the default path)."""
    _run(["git", "init", "-q"], cwd=scratch_repo, check=False)
    refspecs = [f"refs/tags/{t}:refs/tags/{t}" for t in tags]
    result = _run(["git", "fetch", "--no-tags", source, *refspecs], cwd=scratch_repo)
    if result.returncode != 0:
        raise FetchError(f"git fetch failed for tags {tags}: {result.stderr.strip()}")


def verify_tag_signature(scratch_repo: Path, tag: str, gnupg_home: Path, pinned_fingerprint: str) -> str:
    """G5 + G6: run `git verify-tag --raw` against the isolated, pinned-only
    keyring and require a VALIDSIG line whose fingerprint equals the pinned
    fingerprint. Any of: missing tag, unsigned tag, tampered signature,
    signature by a key that is not present in the pinned-only keyring, or a
    VALIDSIG fingerprint that mismatches -> FetchError (fail closed).
    Returns the verified signer fingerprint on success."""
    env_result = _run(
        ["git", "-c", "gpg.program=gpg", "verify-tag", "--raw", tag],
        cwd=scratch_repo,
        env=_gnupg_env(gnupg_home),
    )
    raw_output = env_result.stdout + "\n" + env_result.stderr
    validsig_fprs = [m.group(1) for m in (VALIDSIG_RE.match(line) for line in raw_output.splitlines()) if m]

    if env_result.returncode != 0 or not validsig_fprs:
        raise FetchError(f"tag {tag!r} failed signature verification (no VALIDSIG): {raw_output.strip()}")

    signer_fpr = validsig_fprs[0]
    if signer_fpr != pinned_fingerprint:
        raise FetchError(
            f"tag {tag!r} was signed by {signer_fpr}, which does not match the pinned "
            f"fingerprint {pinned_fingerprint} -- refusing (wrong-key signature)"
        )
    return signer_fpr


def _gnupg_env(gnupg_home: Path) -> dict:
    import os

    env = dict(os.environ)
    env["GNUPGHOME"] = str(gnupg_home)
    return env


def resolve_tag_commit(scratch_repo: Path, tag: str) -> str:
    result = _run(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=scratch_repo)
    if result.returncode != 0:
        raise FetchError(f"could not resolve tag {tag!r} to a commit: {result.stderr.strip()}")
    return result.stdout.strip()


def archive_tree(scratch_repo: Path, commit_sha: str, dest_dir: Path) -> None:
    """Single local `git archive | tar -x` extraction -- no per-file reads."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "archive", commit_sha], cwd=scratch_repo, stdout=subprocess.PIPE
    )
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest_dir)], stdin=archive.stdout, capture_output=True
    )
    archive.stdout.close()
    archive.wait()
    if archive.returncode != 0 or extract.returncode != 0:
        raise FetchError(
            f"git archive/tar extraction failed for {commit_sha}: "
            f"archive_rc={archive.returncode} tar_rc={extract.returncode} {extract.stderr.decode(errors='replace')}"
        )


def verify_manifest_blobs(extracted_dir: Path) -> dict:
    """Per-file SHA-256 gate: load engine/manifest.json from the extracted,
    signature-verified tree and recompute every listed file's hash. ANY
    mismatch, or ANY manifest path that fails allowlist/path-traversal
    validation, aborts the whole fetch (fail closed) -- never trust the
    manifest's own path claims, same posture as pull.py's validate_path."""
    manifest_path = extracted_dir / ENGINE_MANIFEST_RELPATH
    if not manifest_path.is_file():
        raise FetchError(f"extracted tree has no {ENGINE_MANIFEST_RELPATH}")

    with open(manifest_path) as f:
        manifest = json.load(f)

    includes, excludes = manifest_mod.read_allowlist()
    files = manifest.get("files", {})
    for relpath, claimed_hash in sorted(files.items()):
        # Canonicalize at the trust boundary (security-review finding,
        # D#1586 Batch B2b fix round): reject any manifest key that is not
        # already in canonical form (fail closed, no silent normalization).
        # This is the SAME gate pull.py's validate_path applies -- keeping a
        # poisoned non-canonical key (e.g. `scripts/engine-sync/./pull.py`)
        # out of a verified fetch means it can never even reach apply.py's
        # classify/G4 stage in the first place.
        canonical, reason = pull_mod.canonicalize_relpath(relpath)
        if canonical is None:
            raise FetchError(f"manifest path rejected ({reason}): {relpath!r}")

        p = Path(canonical)
        if p.is_absolute() or ".." in p.parts:
            raise FetchError(f"manifest path fails traversal check: {relpath!r}")
        if not manifest_mod.is_included(canonical, includes) or manifest_mod.is_excluded(canonical, excludes):
            raise FetchError(f"manifest path is not covered by the allowlist (or is denied): {relpath!r}")

        blob_path = extracted_dir / relpath
        try:
            resolved = blob_path.resolve()
            resolved.relative_to(extracted_dir.resolve())
        except ValueError:
            raise FetchError(f"manifest path resolves outside the extracted tree: {relpath!r}")
        if blob_path.is_symlink():
            raise FetchError(f"manifest path is a symlink in the fetched tree (rejected): {relpath!r}")
        if not blob_path.is_file():
            raise FetchError(f"manifest-claimed file missing from fetched tree: {relpath!r}")

        actual_hash = manifest_mod.sha256_of(blob_path)
        if actual_hash != claimed_hash:
            raise FetchError(
                f"per-file SHA-256 mismatch for {relpath!r}: manifest claims {claimed_hash}, "
                f"actual fetched content hashes to {actual_hash}"
            )

    return manifest


def fetch_and_verify_tag(scratch_repo: Path, tag: str, gnupg_home: Path, pinned_fingerprint: str, staging_root: Path) -> dict:
    """Full chain for one tag: signature verify -> resolve -> archive ->
    per-file SHA-256 verify. Returns {"tag", "commit_sha", "signer_fingerprint",
    "manifest", "dir"} where "dir" is a still-staged (not yet committed to
    --out) extracted+verified tree."""
    signer_fpr = verify_tag_signature(scratch_repo, tag, gnupg_home, pinned_fingerprint)
    commit_sha = resolve_tag_commit(scratch_repo, tag)
    extracted_dir = staging_root / f"extracted-{tag}"
    archive_tree(scratch_repo, commit_sha, extracted_dir)
    manifest = verify_manifest_blobs(extracted_dir)
    return {
        "tag": tag,
        "commit_sha": commit_sha,
        "signer_fingerprint": signer_fpr,
        "engine_version": manifest.get("engine_version"),
        "dir": extracted_dir,
    }


def resolve_baseline_tag(applied_json: Path | None, explicit_baseline_tag: str | None, tag_prefix: str) -> str | None:
    """Baseline tag resolution per Spec item 5: derived from the sibling's
    engine/applied.json engine_version, unless an explicit --baseline-tag is
    given (used directly by tests / manual invocation). No applied.json and
    no explicit tag -> first adoption -> baseline fetch is skipped (not an
    error, matches pull.py's first_adoption semantics)."""
    if explicit_baseline_tag:
        return explicit_baseline_tag
    if applied_json is None or not applied_json.is_file():
        return None
    with open(applied_json) as f:
        applied = json.load(f)
    version = applied.get("engine_version")
    if not version:
        return None
    return f"{tag_prefix}{version}"


def run_fetch(
    source: str,
    target_tag: str,
    trust_path: Path,
    out_dir: Path,
    baseline_tag: str | None = None,
    applied_json: Path | None = None,
    tag_prefix: str = "",
) -> dict:
    """Orchestrates the full verified fetch. Returns the fetch report dict on
    success. Raises FetchError on ANY failure -- caller (cmd_fetch) is
    responsible for guaranteeing --out was never touched in that case, which
    is true by construction here: nothing is written under out_dir until
    every verification step below has already succeeded."""
    trust = load_trust(trust_path)
    resolved_baseline_tag = resolve_baseline_tag(applied_json, baseline_tag, tag_prefix)

    with tempfile.TemporaryDirectory(prefix="engine-sync-fetch-") as tmp:
        tmp_path = Path(tmp)
        gnupg_home = tmp_path / "gnupg"
        gnupg_home.mkdir(mode=0o700)
        scratch_repo = tmp_path / "scratch-repo"
        scratch_repo.mkdir()
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        import_pinned_key(gnupg_home, trust["public_key_armored"], trust["pinned_fingerprint"])

        tags_to_fetch = [target_tag] + ([resolved_baseline_tag] if resolved_baseline_tag else [])
        git_fetch_tags(scratch_repo, source, tags_to_fetch)

        target_result = fetch_and_verify_tag(
            scratch_repo, target_tag, gnupg_home, trust["pinned_fingerprint"], staging_root
        )
        baseline_result = None
        if resolved_baseline_tag:
            baseline_result = fetch_and_verify_tag(
                scratch_repo, resolved_baseline_tag, gnupg_home, trust["pinned_fingerprint"], staging_root
            )

        # Every verification step above succeeded -- this is the ONLY point
        # at which we touch --out. Everything before this line lived in the
        # TemporaryDirectory, which is unconditionally cleaned up on any
        # exception raised above (fail-closed: zero files written to --out).
        out_dir.mkdir(parents=True, exist_ok=True)
        target_out = out_dir / "target"
        if target_out.exists():
            shutil.rmtree(target_out)
        shutil.move(str(target_result["dir"]), str(target_out))

        base_out_relpath = None
        if baseline_result is not None:
            base_out = out_dir / "base"
            if base_out.exists():
                shutil.rmtree(base_out)
            shutil.move(str(baseline_result["dir"]), str(base_out))
            base_out_relpath = "base"

        report = {
            "source": source,
            "target": {
                "tag": target_result["tag"],
                "commit_sha": target_result["commit_sha"],
                "engine_version": target_result["engine_version"],
                "signer_fingerprint": target_result["signer_fingerprint"],
                "dir": "target",
            },
            "baseline": (
                {
                    "tag": baseline_result["tag"],
                    "commit_sha": baseline_result["commit_sha"],
                    "engine_version": baseline_result["engine_version"],
                    "signer_fingerprint": baseline_result["signer_fingerprint"],
                    "dir": base_out_relpath,
                }
                if baseline_result is not None
                else None
            ),
            "pinned_fingerprint": trust["pinned_fingerprint"],
        }
        with open(out_dir / "fetch-report.json", "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
        return report


def cmd_fetch(args: argparse.Namespace) -> int:
    try:
        report = run_fetch(
            source=args.source,
            target_tag=args.target_tag,
            trust_path=Path(args.trust_file),
            out_dir=Path(args.out),
            baseline_tag=args.baseline_tag,
            applied_json=Path(args.applied_json) if args.applied_json else None,
            tag_prefix=args.tag_prefix,
        )
    except FetchError as exc:
        print(f"error: verified fetch failed: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description=(
            "Slice B2a: verified signed-tag -> pinned-key -> SHA -> per-file-SHA-256 "
            "fetch of an engine release (and optionally its baseline) into a local "
            "mirror directory. Deterministic, zero-LLM, zero writes to any sibling "
            "working tree. Fails closed (non-zero exit, --out untouched) on any "
            "verification failure."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_parser = sub.add_parser(
        "fetch",
        help="Verify and materialize a target tag (and optional baseline tag) into --out.",
    )
    fetch_parser.add_argument(
        "--source", required=True, help="Upstream git remote (URL or local path) to fetch tags from."
    )
    fetch_parser.add_argument("--target-tag", required=True, help="The engine release tag to fetch and verify.")
    fetch_parser.add_argument(
        "--trust-file",
        required=True,
        help="Path to engine/trust.json (pinned_fingerprint + public_key_armored). "
        "TOFU-pinned once by a human; rotation is a human-gated runbook step, never silent re-TOFU.",
    )
    fetch_parser.add_argument(
        "--out",
        required=True,
        help="Directory to materialize the verified target/ (and base/) trees into. "
        "Untouched unless every verification step succeeds.",
    )
    fetch_parser.add_argument(
        "--baseline-tag",
        default=None,
        help="Explicit baseline tag to also fetch+verify (yields the common-ancestor "
        "blob for three-way merge). Overrides --applied-json if both are given.",
    )
    fetch_parser.add_argument(
        "--applied-json",
        default=None,
        help="Path to the sibling's engine/applied.json; its engine_version is used to "
        "derive the baseline tag when --baseline-tag is not given. Missing file or no "
        "engine_version -> first adoption -> baseline fetch is skipped (not an error).",
    )
    fetch_parser.add_argument(
        "--tag-prefix",
        default="",
        help="Prefix prepended to an applied.json-derived engine_version to form the "
        "baseline tag name (e.g. 'v' if tags are 'v0.1.0' but engine_version is '0.1.0'). "
        "Ignored when --baseline-tag is given explicitly.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return cmd_fetch(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
