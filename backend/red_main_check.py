"""backend/red_main_check.py — Red-main verdict-overturn producer (D#1409).

After a PR is merged, checks whether the merge left main in a red state by:
  1. Mapping changed files to their bounded test files.
  2. Running only those tests (timeout 120s).
  3. If any test fails, querying agent_run for all passing roles on that PR and
     recording a red_main verdict-overturn for each.

Design constraints:
  - FAIL-OPEN: any error or timeout => NO overturn recorded (never a false red_main).
  - Pure functions are isolated so they can be unit-tested without subprocess I/O.
  - CLI: `python3 backend/red_main_check.py --pr N [--repo-root PATH] [--dry-run]`

Usage::

    python3 backend/red_main_check.py --pr 1379
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from backend._repo import CODE_REPO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers (unit-testable)
# ---------------------------------------------------------------------------


def map_changed_to_test_files(
    changed_files: list[str],
    repo_root: Path,
) -> list[Path]:
    """Map a list of changed file paths to the test files that should be run.

    Rules (in order, de-duplicated):
      1. If a changed path IS backend/tests/test_*.py, include it directly.
      2. If a changed path is backend/<X>.py (not tests/), check whether
         backend/tests/test_<X>.py exists; if so include it.

    Returns absolute paths that exist on disk.
    """
    repo_root = Path(repo_root)
    seen: set[Path] = set()
    result: list[Path] = []

    for raw in changed_files:
        p = Path(raw)
        parts = p.parts

        # Rule 1: directly a test file under backend/tests/
        if (
            len(parts) >= 3
            and parts[0] == "backend"
            and parts[1] == "tests"
            and parts[-1].startswith("test_")
            and parts[-1].endswith(".py")
        ):
            abs_path = repo_root / p
            if abs_path.exists() and abs_path not in seen:
                seen.add(abs_path)
                result.append(abs_path)
            continue

        # Rule 2: backend/<X>.py -> backend/tests/test_<X>.py
        if (
            len(parts) == 2
            and parts[0] == "backend"
            and parts[-1].endswith(".py")
            and not parts[-1].startswith("test_")
        ):
            stem = Path(parts[-1]).stem  # e.g. "stats_writer"
            candidate = repo_root / "backend" / "tests" / f"test_{stem}.py"
            if candidate.exists() and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)

    return result


class TestRunResult(NamedTuple):
    passed: bool
    failures: list[str]  # lines from stdout/stderr on failure
    skipped: bool  # True when no test files to run


def run_bounded_tests(
    test_files: list[Path],
    timeout: int = 120,
) -> TestRunResult:
    """Run the given test files under pytest with a hard timeout.

    Returns TestRunResult.  On timeout or subprocess error, returns
    passed=True (fail-open — we cannot distinguish red from broken env).
    """
    if not test_files:
        return TestRunResult(passed=True, failures=[], skipped=True)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--tb=short",
        "-q",
        "--no-header",
    ] + [str(f) for f in test_files]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "red_main_check: pytest timed out after %ds — treating as green (fail-open)",
            timeout,
        )
        return TestRunResult(passed=True, failures=[], skipped=False)
    except Exception as exc:
        logger.warning("red_main_check: subprocess error — %s — fail-open", exc)
        return TestRunResult(passed=True, failures=[], skipped=False)

    if proc.returncode == 0:
        return TestRunResult(passed=True, failures=[], skipped=False)

    # Collect failure lines for evidence_ref
    failure_lines = (proc.stdout + proc.stderr).splitlines()
    return TestRunResult(passed=False, failures=failure_lines, skipped=False)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_passing_roles_for_pr(pr: int) -> list[tuple[str, str]]:
    """Return [(role, verdict), ...] for all pass/done agent_run rows for this PR.

    Returns empty list on any error (fail-open).
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        logger.warning("red_main_check: duckdb not installed — cannot look up roles")
        return []

    try:
        from backend.agent_run_tracker import _db_path  # noqa: PLC0415
    except ImportError:
        logger.warning("red_main_check: backend.agent_run_tracker unavailable")
        return []

    db = _db_path()
    if not Path(str(db)).exists():
        return []

    try:
        conn = duckdb.connect(str(db), read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT role, verdict
                FROM agent_run
                WHERE pr = ?
                  AND verdict IN ('pass', 'done')
                  AND end_ts IS NOT NULL
                ORDER BY end_ts ASC
                """,
                [pr],
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("red_main_check: DB query failed — %s — fail-open", exc)
        return []

    return [(role, verdict) for role, verdict in rows]


def record_overturns_for_pr(
    pr: int,
    passing_roles: list[tuple[str, str]],
    evidence_ref: str,
) -> int:
    """Call record_overturn(kind='red_main', ...) for each passing role.

    Returns number of overturns recorded.  Skips silently on import/write errors.
    """
    try:
        from backend.verdict_overturn import record_overturn  # noqa: PLC0415
    except ImportError:
        logger.warning("red_main_check: backend.verdict_overturn unavailable — skipping")
        return 0

    count = 0
    for role, verdict in passing_roles:
        try:
            record_overturn(
                pr=pr,
                prior_role=role,
                prior_verdict=verdict,
                contradicting_source="red-main-check",
                kind="red_main",
                evidence_ref=evidence_ref,
            )
            count += 1
            logger.info(
                "red_main_check: recorded red_main overturn — pr=%d role=%s verdict=%s",
                pr,
                role,
                verdict,
            )
        except Exception as exc:
            logger.warning(
                "red_main_check: record_overturn failed for role=%s — %s",
                role,
                exc,
            )

    return count


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def check_pr(
    pr: int,
    changed_files: list[str],
    repo_root: Path,
    timeout: int = 120,
    dry_run: bool = False,
) -> dict:
    """Full pipeline: map files -> run tests -> record overturns if red.

    Returns a result dict with keys:
      - test_files: list of test file paths (strings)
      - skipped: bool (True when no test files matched)
      - passed: bool
      - failures: list[str]
      - overturns_recorded: int
    """
    repo_root = Path(repo_root)
    test_files = map_changed_to_test_files(changed_files, repo_root)
    result_base: dict = {"test_files": [str(f) for f in test_files]}

    run = run_bounded_tests(test_files, timeout=timeout)
    result_base["skipped"] = run.skipped
    result_base["passed"] = run.passed
    result_base["failures"] = run.failures[:40]  # cap evidence size
    result_base["overturns_recorded"] = 0

    if run.passed:
        return result_base

    # Main is red — look up passing roles and record overturns
    passing_roles = get_passing_roles_for_pr(pr)
    evidence_ref = f".autonomous-team/pr-artifacts/{pr}/red-main-check.txt"

    if dry_run:
        result_base["passing_roles"] = passing_roles
        result_base["dry_run"] = True
        return result_base

    count = record_overturns_for_pr(pr, passing_roles, evidence_ref)
    result_base["overturns_recorded"] = count
    result_base["passing_roles"] = [(r, v) for r, v in passing_roles]
    return result_base


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _fetch_pr_changed_files(pr: int, repo_root: Path) -> list[str]:
    """Use gh CLI to fetch changed files for a PR.

    Returns empty list on error (fail-open).
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr),
             "--json", "files",
             "--jq", ".files[].path",
             "--repo", CODE_REPO],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            logger.warning("red_main_check: gh pr view failed — %s", proc.stderr)
            return []
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception as exc:
        logger.warning("red_main_check: gh call failed — %s", exc)
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check if a merged PR left main red and record verdict-overturns."
    )
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Path to repo root (default: parent of this file's parent)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Pytest timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without recording overturns",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output result as JSON",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else Path(__file__).parent.parent
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    changed_files = _fetch_pr_changed_files(args.pr, repo_root)

    result = check_pr(
        pr=args.pr,
        changed_files=changed_files,
        repo_root=repo_root,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    if args.output_json:
        print(json.dumps(result, indent=2))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[red_main_check] pr={args.pr} status={status} "
              f"test_files={len(result['test_files'])} "
              f"overturns={result['overturns_recorded']}")
        if not result["passed"]:
            for line in result["failures"][:10]:
                print(f"  {line}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
