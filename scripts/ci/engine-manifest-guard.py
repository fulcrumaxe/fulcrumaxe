#!/usr/bin/env python3
"""engine-manifest-guard.py — CI guard for engine/manifest.json drift (D#1928).

Background
----------
`engine/manifest.json` pins a sha256 for every file `scripts/engine-sync/
manifest.py`'s allowlist considers in-scope. `pull.py` trusts those pins to
decide whether an upstream file may be adopted into a downstream repo — a
stale pin is a pin that cannot adjudicate anything. Nothing in this repo ever
ran `manifest.py verify` in CI, so the manifest went 67 days without a full
regeneration while two merged commits documented, in their own commit
bodies, hand-editing it instead of running the generator because a full
regenerate's diff was unreviewably large.

Why this is not just `manifest.py verify`
------------------------------------------
`cmd_verify` only ever iterated `manifest["files"]`. A file that matches the
allowlist but was never pinned was invisible to it — not merely unreported,
structurally unreachable — so 53-75 such files (see D#1928's measurement
table; the exact count depends on which tree and when) accumulated with
`verify` reporting `clean` the whole time. `cmd_verify` itself now also
carries `added`-category detection (this same Discussion), but this guard is
a second, independent computation of the same comparison rather than a thin
wrapper around it — the Implementation Notes call for calling `collect_files()`
directly, and doing that here rather than shelling out to `manifest.py verify`
means a future regression in `cmd_verify`'s plumbing (argument parsing, exit
codes, an accidental early return) does not also blind this guard.

What this checks
-----------------
Recomputes the live allowlist candidate set with `collect_files()` (the real
matcher — see `scripts/engine-sync/tests/test_coldstart_boundary.py` for the
precedent of importing it rather than reimplementing the globbing) and
compares it against the committed manifest's pinned entries. Reports, by
name:

  drifted  — pinned, file present, hash no longer matches
  missing  — pinned, file no longer exists
  added    — matches the allowlist, was never pinned

Two failure-closed floors, independent of the comparison above: an allowlist
that matches zero candidate files, and a manifest with no usable `files`
entries, each fail rather than vacuously agreeing — see D#1928 item 3, where
`verify` used to report `clean (0 files match)` on exactly that input, which
is indistinguishable from a manifest that pins everything and matches.

`run_self_test()` proves this guard's own comparison logic can fail before
trusting its verdict on the real repo: three independently-constructed
fixtures (a drifted pin, an unpinned candidate, a pin whose file is gone)
must each report their offending path by name, and two further fixtures (an
allowlist matching nothing, a manifest with an empty `files` key) must each
refuse rather than pass. A self-test that cannot fail would make a green run
here worse than no guard at all — see the file-scope discussion in
`scripts/ci/coldstart-state-dir-guard.py`, which sets the precedent for this
run-self-test-first shape.

Usage
-----
    python3 scripts/ci/engine-manifest-guard.py

Exit 0: self-test passed, and the real repo's candidate set exactly matches
        its pinned manifest (0 drifted, 0 missing, 0 added).
Exit 1: a self-test fixture did not discriminate the way it must, OR the real
        repo has drift/missing/added entries, OR a real-repo floor tripped
        (nothing pinned, nothing matched by the allowlist).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_MODULE_PATH = REPO_ROOT / "scripts" / "engine-sync" / "manifest.py"


def _load_manifest_module():
    """Import scripts/engine-sync/manifest.py as a library, matching the
    precedent in scripts/engine-sync/tests/test_manifest.py and pull.py:49-54
    -- collect_files()/read_allowlist() are the candidate-set builder, not a
    contract to be re-implemented here."""
    spec = importlib.util.spec_from_file_location("engine_manifest_guard_target", MANIFEST_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _compare(mod, root: Path, includes: list[str], excludes: list[str], pinned: dict) -> tuple[int, list[str]]:
    """Run the real comparison: recompute candidates under `root`, diff
    against `pinned`. Returns (candidate_count, [problem descriptions]).

    Shared by run_self_test() (fixture roots) and main() (the real repo) so
    the self-test proves the exact code path the real check uses, not a
    parallel reimplementation of it.
    """
    candidates = mod.collect_files(root, includes, excludes)
    problems: list[str] = []

    if not candidates:
        problems.append("allowlist matched zero candidate files -- refusing a vacuous pass")
        return 0, problems
    if not isinstance(pinned, dict) or not pinned:
        problems.append("manifest has no usable 'files' entries -- refusing a vacuous pass")
        return len(candidates), problems

    drifted: list[str] = []
    missing: list[str] = []
    for relpath, recorded in sorted(pinned.items()):
        full = root / relpath
        if not full.is_file():
            missing.append(relpath)
            continue
        if mod.sha256_of(full) != recorded:
            drifted.append(relpath)
    added = sorted(set(candidates) - set(pinned))

    for p in drifted:
        problems.append(f"drifted: {p}")
    for p in missing:
        problems.append(f"missing: {p}")
    for p in added:
        problems.append(f"added: {p}")
    return len(candidates), problems


def run_self_test(mod, fail) -> None:
    includes = ["scripts/*.sh"]
    excludes: list[str] = []

    # (a) a pinned file whose content changed
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        target = root / "scripts" / "watched.sh"
        target.write_text("echo original\n")
        original_hash = mod.sha256_of(target)
        target.write_text("echo tampered\n")
        _, problems = _compare(mod, root, includes, excludes, {"scripts/watched.sh": original_hash})
        if not any(p == "drifted: scripts/watched.sh" for p in problems):
            fail(f"self-test: drifted fixture — expected 'drifted: scripts/watched.sh', got {problems}")

    # (b) a file matching the allowlist with no pin
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        pinned_file = root / "scripts" / "pinned.sh"
        pinned_file.write_text("echo pinned\n")
        (root / "scripts" / "unpinned.sh").write_text("echo new\n")
        pinned_hash = mod.sha256_of(pinned_file)
        _, problems = _compare(mod, root, includes, excludes, {"scripts/pinned.sh": pinned_hash})
        if not any(p == "added: scripts/unpinned.sh" for p in problems):
            fail(f"self-test: added fixture — expected 'added: scripts/unpinned.sh', got {problems}")
        if any(p.startswith(("drifted:", "missing:")) for p in problems):
            fail(f"self-test: added fixture — unexpected non-added problem(s): {problems}")

    # (c) a pin whose file is gone. A second, still-present candidate keeps
    # the candidate set non-empty so this fixture exercises the missing-file
    # path rather than tripping the (d) zero-candidate floor below.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "still-here.sh").write_text("echo present\n")
        still_here_hash = mod.sha256_of(root / "scripts" / "still-here.sh")
        gone = root / "scripts" / "gone.sh"
        gone.write_text("echo will be removed\n")
        gone_hash = mod.sha256_of(gone)
        gone.unlink()
        _, problems = _compare(
            mod,
            root,
            includes,
            excludes,
            {"scripts/gone.sh": gone_hash, "scripts/still-here.sh": still_here_hash},
        )
        if not any(p == "missing: scripts/gone.sh" for p in problems):
            fail(f"self-test: missing fixture — expected 'missing: scripts/gone.sh', got {problems}")
        if any(p.startswith("added:") for p in problems):
            fail(f"self-test: missing fixture — unexpected 'added' problem(s): {problems}")

    # (d) floor: allowlist matches nothing -> refuse, not a vacuous pass
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "irrelevant.py").write_text("# not matched by *.sh\n")
        count, problems = _compare(mod, root, includes, excludes, {"scripts/anything.sh": "0" * 64})
        if count != 0 or not any("zero candidate files" in p for p in problems):
            fail(f"self-test: empty-candidate floor — expected a zero-candidate refusal, got count={count} problems={problems}")

    # (e) floor: manifest has no usable 'files' entries -> refuse
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "present.sh").write_text("echo present\n")
        count, problems = _compare(mod, root, includes, excludes, {})
        if count == 0 or not any("no usable 'files' entries" in p for p in problems):
            fail(f"self-test: empty-manifest floor — expected a no-usable-entries refusal, got count={count} problems={problems}")


def main() -> int:
    mod = _load_manifest_module()

    failures: list[str] = []

    def fail(msg: str) -> None:
        failures.append(msg)
        print(f"FAIL {msg}", file=sys.stderr)

    run_self_test(mod, fail)
    if failures:
        print(
            f"engine-manifest-guard: {len(failures)} self-test failure(s) -- "
            f"refusing to trust the real check",
            file=sys.stderr,
        )
        return 1

    if not mod.MANIFEST_PATH.exists():
        print(f"engine-manifest-guard: FAIL -- manifest not found at {mod.MANIFEST_PATH}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(mod.MANIFEST_PATH.read_text())
    except json.JSONDecodeError as exc:
        print(f"engine-manifest-guard: FAIL -- manifest at {mod.MANIFEST_PATH} is not readable JSON: {exc}", file=sys.stderr)
        return 1

    includes, excludes = mod.read_allowlist()
    pinned = manifest.get("files")
    candidate_count, problems = _compare(mod, mod.REPO_ROOT, includes, excludes, pinned)
    pinned_count = len(pinned) if isinstance(pinned, dict) else 0

    if problems:
        for p in problems:
            print(f"engine-manifest-guard: FAIL -- {p}", file=sys.stderr)
        print(
            f"engine-manifest-guard: {len(problems)} problem(s) across "
            f"{candidate_count} candidate(s) / {pinned_count} pinned entries",
            file=sys.stderr,
        )
        return 1

    print(f"engine-manifest-guard: OK ({candidate_count} candidates, {pinned_count} pinned, 0 drifted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
