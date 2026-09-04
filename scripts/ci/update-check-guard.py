#!/usr/bin/env python3
"""scripts/ci/update-check-guard.py — behavioral CI guard for update-check.sh
(D#2335 Spec, PR 1, acceptance item 13).

Modeled on scripts/ci/registry-queue-count-guard.py: this is NOT a lint over
source text. It builds a scratch tree in a tmpdir, writes an
engine-install.json baseline stamp into it, and drives the real
scripts/update-check.sh subprocess with UPDATE_CHECK_UPSTREAM_CMD set to a
small fixture command for each of the four verdicts, asserting both the
exit code AND the message content the Spec requires (acceptance items
1-8). No network call and no GH_TOKEN are needed anywhere in this file —
every scenario that would otherwise call `gh api` is short-circuited by
UPDATE_CHECK_UPSTREAM_CMD, and the no-baseline scenario never reaches the
upstream-resolution code path at all.

Run from the repo root:

    python3 scripts/ci/update-check-guard.py

Exit 0: every assertion passed.
Exit 1: at least one assertion failed. Details printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UPDATE_CHECK = REPO_ROOT / "scripts" / "update-check.sh"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(condition: bool, description: str) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {description}")
    else:
        FAIL += 1
        FAILURES.append(description)
        print(f"  FAIL: {description}")


def make_case(name: str, root: Path) -> Path:
    """Build a scratch tree at root/name that looks like a bootstrapped repo
    from update-check.sh's point of view: it resolves its own repo root as
    "the directory two levels above this script" (dirname/.. of
    scripts/update-check.sh), so a real copy of the script has to live at
    <case_dir>/scripts/update-check.sh for that resolution to land on
    <case_dir> instead of this guard's own checkout."""
    case_dir = root / name
    (case_dir / "scripts").mkdir(parents=True)
    shutil.copy2(UPDATE_CHECK, case_dir / "scripts" / "update-check.sh")
    return case_dir


def run(env_extra: dict, cwd: Path, args: list[str] | None = None) -> subprocess.CompletedProcess:
    # Inherit the real environment (needed to find bash/python3/etc. across
    # hosts) but strip GH_TOKEN — this guard must prove it needs neither
    # network nor a token, and every scenario below either short-circuits
    # via UPDATE_CHECK_UPSTREAM_CMD or never reaches the upstream-resolution
    # code path (no-baseline, usage errors) in the first place.
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.update(env_extra)
    cmd = ["bash", str(cwd / "scripts" / "update-check.sh")] + (args or [])
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30)


def write_stamp(target: Path, engine_commit: str = "1111111111111111111111111111111111111111") -> None:
    autonomous_team = target / ".autonomous-team"
    autonomous_team.mkdir(parents=True, exist_ok=True)
    stamp = {
        "engine_version": "0.3.0",
        "engine_commit": engine_commit,
        "source": "clone",
        "source_repo": "acme/fixture-engine",
        "bootstrapped_at": "2026-09-04T00:00:00Z",
    }
    (autonomous_team / "engine-install.json").write_text(json.dumps(stamp, indent=2) + "\n")


def main() -> int:
    if not UPDATE_CHECK.is_file():
        print(f"FAIL: {UPDATE_CHECK} does not exist", file=sys.stderr)
        return 1

    messages: dict[str, str] = {}
    codes: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix="update-check-guard.") as tmp:
        tmp_path = Path(tmp)

        # --- assertion 1: --help exits 0, names all four exit codes -------
        help_case_dir = make_case("help", tmp_path)
        result = run({}, help_case_dir, ["--help"])
        check(result.returncode == 0, "--help exits 0")
        for code_str in ("0 ", "10 ", "20 ", "2 "):
            check(code_str in result.stdout, f"--help usage text names exit code {code_str.strip()}")

        # --- assertion 2: up to date ---------------------------------------
        case_dir = make_case("up-to-date", tmp_path)
        write_stamp(case_dir)
        result = run({"UPDATE_CHECK_UPSTREAM_CMD": "echo 0"}, case_dir)
        codes["up_to_date"] = result.returncode
        messages["up_to_date"] = result.stdout.strip()
        check(result.returncode == 0, "up-to-date case exits 0")
        check("up to date" in result.stdout, "up-to-date case message contains 'up to date'")

        # --- assertion 3: update available, correct commit count ----------
        case_dir = make_case("update-available", tmp_path)
        write_stamp(case_dir)
        result = run({"UPDATE_CHECK_UPSTREAM_CMD": "echo 5"}, case_dir)
        codes["update_available"] = result.returncode
        messages["update_available"] = result.stdout.strip()
        check(result.returncode == 10, "update-available case exits 10")
        check("update available" in result.stdout, "update-available case message contains 'update available'")
        check(re.search(r"\b5\b", result.stdout) is not None, "update-available case message names the correct commit count (5)")

        # --- assertion 4: no baseline recorded ------------------------------
        case_dir = make_case("no-baseline", tmp_path)
        result = run({}, case_dir)
        codes["no_baseline"] = result.returncode
        messages["no_baseline"] = result.stderr.strip()
        check(result.returncode == 20, "no-baseline case exits 20")
        check("cannot determine" in result.stderr, "no-baseline case message contains 'cannot determine'")
        check("reason=no_baseline_recorded" in result.stderr, "no-baseline case carries reason=no_baseline_recorded")
        check("up to date" not in result.stderr, "no-baseline case message does NOT contain 'up to date'")

        # --- assertion 5: upstream unreachable ------------------------------
        case_dir = make_case("unreachable", tmp_path)
        write_stamp(case_dir)
        result = run({"UPDATE_CHECK_UPSTREAM_CMD": "exit 1"}, case_dir)
        messages["unreachable"] = result.stderr.strip()
        check(result.returncode == 20, "upstream-unreachable case exits 20")
        check("reason=upstream_unreachable" in result.stderr, "upstream-unreachable case carries reason=upstream_unreachable")

        # --- assertion 6: baseline not in upstream (404-equivalent) --------
        case_dir = make_case("not-in-upstream", tmp_path)
        write_stamp(case_dir)
        result = run({"UPDATE_CHECK_UPSTREAM_CMD": "echo NOT_FOUND"}, case_dir)
        messages["not_in_upstream"] = result.stderr.strip()
        check(result.returncode == 20, "baseline-not-in-upstream case exits 20 (not 0, not 10)")
        check("reason=baseline_not_in_upstream" in result.stderr, "baseline-not-in-upstream case carries reason=baseline_not_in_upstream")
        check(
            messages["not_in_upstream"] != messages["unreachable"],
            "baseline-not-in-upstream message differs from upstream-unreachable message",
        )
        check(
            messages["not_in_upstream"] != messages["no_baseline"],
            "baseline-not-in-upstream message differs from no-baseline message",
        )

        # --- assertion 7: usage error on unknown flag -----------------------
        case_dir = make_case("bogus-flag", tmp_path)
        result = run({}, case_dir, ["--bogus-flag"])
        check(result.returncode == 2, "unknown flag exits 2")
        check(result.stderr.strip() != "", "unknown flag prints a message on stderr")
        check(result.stdout.strip() == "", "unknown flag prints nothing on stdout")

        # --- assertion 8: pairwise distinctness -----------------------------
        verdict_messages = [messages["up_to_date"], messages["update_available"], messages["no_baseline"]]
        check(len(set(verdict_messages)) == 3, "up-to-date / update-available / no-baseline messages are pairwise distinct")
        verdict_codes = [codes["up_to_date"], codes["update_available"], codes["no_baseline"]]
        check(len(set(verdict_codes)) == 3, "up-to-date / update-available / no-baseline exit codes are pairwise distinct (0, 10, 20)")

        # --- assertion 9: --record-baseline writes the stamp, enables case 2/3
        case_dir = make_case("record-baseline", tmp_path)
        good_sha = "2222222222222222222222222222222222222222"
        result = run({}, case_dir, ["--record-baseline", good_sha])
        check(result.returncode == 0, "--record-baseline with a valid 40-hex sha exits 0")
        stamp_path = case_dir / ".autonomous-team" / "engine-install.json"
        check(stamp_path.is_file(), "--record-baseline writes .autonomous-team/engine-install.json")
        if stamp_path.is_file():
            written = json.loads(stamp_path.read_text())
            check(written.get("engine_commit") == good_sha, "recorded stamp's engine_commit matches the given sha")
        follow_up = run({"UPDATE_CHECK_UPSTREAM_CMD": "echo 0"}, case_dir)
        check(follow_up.returncode == 0, "update-check succeeds against a --record-baseline'd stamp")

        case_dir = make_case("record-baseline-bad", tmp_path)
        result = run({}, case_dir, ["--record-baseline", "not-a-sha"])
        check(result.returncode == 2, "--record-baseline with a non-40-hex argument exits 2")
        check(
            not (case_dir / ".autonomous-team" / "engine-install.json").exists(),
            "--record-baseline with a bad argument writes nothing",
        )

    print()
    if FAIL:
        print(f"FAIL: {FAIL} assertion(s) failed, {PASS} passed:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"PASS: all {PASS} assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
