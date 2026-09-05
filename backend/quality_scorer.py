"""
quality_scorer.py — Automated code quality scoring for PRs.

Computes a 0-100 quality score for each PR based on four weighted dimensions:
  - Complexity score  (30 pts): Per-function McCabe cyclomatic complexity (averaged)
  - Test coverage     (25 pts): Presence of test files for changed modules
  - Review rounds     (25 pts): How many needs-fix verdicts before pass
  - Size score        (20 pts): Total lines changed

Scores are stored in the blackboard under quality/{pr_number} and optionally
surfaced via the API and dashboard.

Usage (CLI):
    python backend/quality_scorer.py score --pr 53
    python backend/quality_scorer.py score --diff /path/to/file.diff
    python backend/quality_scorer.py history
    python backend/quality_scorer.py stats

Usage (library):
    from backend.quality_scorer import QualityScorer
    scorer = QualityScorer()
    result = scorer.score_diff(diff_text, pr_number=53)
"""

from __future__ import annotations

if __name__ == '__main__' and __package__ is None:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend._repo import CODE_REPO

from backend.blackboard import get_blackboard  # noqa: E402
from backend.event_bus import QualityScoreEvent, get_bus  # noqa: E402

# ---------------------------------------------------------------------------
# Grade mapping
# ---------------------------------------------------------------------------

_GRADE_THRESHOLDS: list[tuple[int, str]] = [
    (95, "A+"),
    (90, "A"),
    (85, "B+"),
    (80, "B"),
    (75, "C+"),
    (70, "C"),
    (60, "D"),
    (0,  "F"),
]


def _to_grade(score: int) -> str:
    """Map a numeric score (0-100) to a letter grade."""
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


# ---------------------------------------------------------------------------
# AST complexity helpers — per-function McCabe cyclomatic complexity
# ---------------------------------------------------------------------------


def _function_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return McCabe cyclomatic complexity for a single function.

    complexity = 1 + count(decision points), where decision points are:
      - ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp
      - Each ast.BoolOp operand beyond the first (A and B and C adds +2)
      - Each 'if' filter inside ast.comprehension (listcomp/setcomp/genexp filters)

    Nested FunctionDef/AsyncFunctionDef subtrees are pruned — they are counted
    separately by _file_complexities, not double-counted here.
    """
    count = 1  # base complexity

    # Walk using explicit queue to allow pruning nested functions
    nodes_to_visit: list[ast.AST] = list(ast.iter_child_nodes(func_node))
    while nodes_to_visit:
        node = nodes_to_visit.pop()

        # Prune nested function definitions — they get their own entry
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)):
            count += 1
        elif isinstance(node, ast.BoolOp):
            # A and B and C has 3 values — adds len(values) - 1 branches
            count += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            # Each 'if' filter clause in a comprehension
            count += len(node.ifs)

        nodes_to_visit.extend(ast.iter_child_nodes(node))

    return count


def _is_pure_data_class(node: ast.ClassDef) -> bool:
    """Return True if a ClassDef should be skipped for complexity purposes.

    Skips pydantic BaseModel subclasses and @dataclass / @dataclasses.dataclass
    decorated classes — these are data containers with no meaningful logic.
    """
    # Check bases for BaseModel
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "BaseModel":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
            return True

    # Check decorators for @dataclass or @dataclasses.dataclass
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "dataclass":
            return True

    return False


def _collect_functions(
    nodes: list[ast.AST],
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Recursively collect all FunctionDef/AsyncFunctionDef nodes.

    Pure-data class bodies (pydantic BaseModel, @dataclass) are skipped entirely.
    Nested functions are included as separate entries (counted once each).
    """
    result: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append(node)
            # Recurse into the function body to find nested functions
            result.extend(_collect_functions(list(ast.iter_child_nodes(node))))
        elif isinstance(node, ast.ClassDef):
            if _is_pure_data_class(node):
                continue  # skip entire class body
            # Recurse into class body
            result.extend(_collect_functions(list(ast.iter_child_nodes(node))))
        elif isinstance(node, ast.AST):
            result.extend(_collect_functions(list(ast.iter_child_nodes(node))))
    return result


def _file_complexities(source: str) -> list[int]:
    """Parse source and return a list of per-function McCabe complexity values.

    Returns [] on SyntaxError or if no functions are found.
    Pure-data class bodies (pydantic BaseModel, @dataclass) are excluded.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    functions = _collect_functions(list(ast.iter_child_nodes(tree)))
    return [_function_complexity(f) for f in functions]


# ---------------------------------------------------------------------------
# Diff parser
# ---------------------------------------------------------------------------


def _parse_diff(diff_text: str) -> dict[str, list[str]]:
    """Parse a unified diff into {filename: [added_lines]}.

    Only lines added (+) are returned, minus the leading '+'.
    Deleted lines are ignored for complexity analysis.

    Deletion-only rule: a pure deletion's "new file" side is /dev/null, so its
    header reads ``+++ /dev/null`` rather than ``+++ b/<path>``. This function
    only assigns ``current_file`` on a ``+++ b/`` header, so a deletion-only
    diff never records an entry for that file at all. That is deliberate, not
    a gap: it means a deletion-only diff has no scorable files and downstream
    scoring reports ``applicable=False`` (the quality gate is skipped
    entirely) rather than penalising a deletion as if it were an untested
    module.
    """
    files: dict[str, list[str]] = {}
    current_file: str | None = None
    added: list[str] = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_file is not None:
                files[current_file] = added
            added = []
            current_file = None
        elif line.startswith("+++ b/"):
            current_file = line[6:]
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])

    if current_file is not None:
        files[current_file] = added

    return files


def _count_diff_lines(diff_text: str) -> int:
    """Count total changed lines (+ and -) in a diff, excluding headers."""
    count = 0
    for line in diff_text.splitlines():
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---")):
            count += 1
    return count


# ---------------------------------------------------------------------------
# QualityScorer
# ---------------------------------------------------------------------------


class QualityScorer:
    """Compute quality scores for PRs and store them in the blackboard."""

    def __init__(self, repo_root: Path | str | None = None) -> None:
        if repo_root is None:
            here = Path(__file__).resolve().parent
            self._repo_root = here.parent
        else:
            self._repo_root = Path(repo_root).resolve()
        self._bb = get_blackboard()
        # Built once per instance (score_diff is called per PR and must not
        # shell out to git repeatedly). None means "could not build" — the
        # coverage dimension reports measured=False in that case rather than
        # scoring every module as uncovered.
        self._test_index: list[str] | None = self._build_test_index()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_pr(
        self,
        pr_number: int,
        discussion: int | None = None,
        cache_ttl_sec: int = 300,
    ) -> dict:
        """Score a PR by number, reading its diff from git.

        Fetches the diff from the GitHub API or falls back to git log.

        If ``cache_ttl_sec`` > 0, checks whether the blackboard already holds a
        score for this PR that was computed against the same head commit SHA and
        within the TTL window.  If so, returns the cached result immediately and
        skips all re-computation (diff fetch, complexity, coverage, etc.).
        Pass ``cache_ttl_sec=0`` to force a full recompute.
        """
        head_sha: str | None = None
        if cache_ttl_sec > 0:
            head_sha = self._fetch_pr_head_sha(pr_number)
            if head_sha:
                cached = self._load_cached_score(pr_number, head_sha, cache_ttl_sec)
                if cached is not None:
                    print(
                        f"[scorer] cache-hit pr=#{pr_number} sha={head_sha[:12]}",
                        file=sys.stderr,
                    )
                    return cached

        diff_text = self._fetch_pr_diff(pr_number)
        result = self.score_diff(diff_text, pr_number=pr_number, discussion=discussion)

        # Stamp head_sha into the result so future callers can validate the cache.
        # score_diff already persisted the result to the blackboard; we overwrite
        # here only to add the head_sha field so the next call can hit the cache.
        if head_sha:
            result["head_sha"] = head_sha
            self._bb.write(f"quality/{pr_number}", result, updated_by="quality-scorer")

        return result

    def score_diff(
        self,
        diff_text: str,
        pr_number: int | None = None,
        discussion: int | None = None,
    ) -> dict:
        """Score a diff string and return the full score dict.

        Optionally stores the result in the blackboard when pr_number is given.

        The result always contains an ``applicable`` boolean:
        - ``True``  — the diff contained scorable files; ``total_score`` is 0-100.
        - ``False`` — no scorable files (e.g. markdown/shell only); ``total_score``
          is ``None`` and ``grade`` is ``"N/A"``.  Callers should skip the quality
          gate entirely when ``applicable`` is False.
        """
        files = _parse_diff(diff_text)
        total_lines = _count_diff_lines(diff_text)

        complexity_result = self._complexity_score(files)
        coverage_result = self._test_coverage_score(files)
        rounds_result = self._review_rounds_score(pr_number)
        size_result = self._size_score(total_lines)

        # Determine applicability: the two content-aware sub-scorers
        # (complexity and coverage) both say "no Python files / no modules" when
        # the diff contains no .py files at all.  Size score is excluded — it
        # counts all lines regardless of file type and always produces a real
        # number even for markdown-only diffs.
        def _is_no_scorable_content(detail: str) -> bool:
            d = detail.lower()
            return (
                ("no " in d and "python files" in d)
                or d == "no modules changed"
            )

        applicable = not all(
            _is_no_scorable_content(r.get("detail", ""))
            for r in [complexity_result, coverage_result]
        )

        if applicable:
            # Renormalise over the maxima of *measured* dimensions only. A
            # dimension that reports measured=False (no real signal — e.g.
            # review_rounds with an empty agent_output/ namespace) is
            # excluded from both the numerator and denominator so it can
            # neither inflate nor deflate the total.
            dims = [complexity_result, coverage_result, rounds_result, size_result]
            measured_dims = [d for d in dims if d.get("measured", True)]
            sum_scores = sum(d["score"] for d in measured_dims)
            sum_maxima = sum(d["max"] for d in measured_dims)
            total: int | None = round(100 * sum_scores / sum_maxima) if sum_maxima else 0
            grade = _to_grade(total)
        else:
            total = None
            grade = "N/A"

        result: dict = {
            "applicable": applicable,
            "pr": pr_number,
            "discussion": discussion,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_score": total,
            "breakdown": {
                "complexity": complexity_result,
                "test_coverage": coverage_result,
                "review_rounds": rounds_result,
                "size": size_result,
            },
            "files_changed": sorted(files.keys()),
            "grade": grade,
        }
        if not applicable:
            result["reason"] = "no scorable files in diff (no .py or test files)"

        if pr_number is not None:
            key = f"quality/{pr_number}"
            self._bb.write(key, result, updated_by="quality-scorer")
            if applicable:
                try:
                    get_bus().publish_async(QualityScoreEvent(
                        source="quality-scorer",
                        pr=pr_number,
                        discussion=discussion,
                        total_score=total,
                        grade=result["grade"],
                    ))
                except Exception:  # noqa: BLE001
                    pass  # never crash the scorer because of event bus issues

        return result

    def history(self, limit: int = 20) -> list[dict]:
        """Return the last *limit* PR scores, most recent first."""
        keys = self._bb.list_keys("quality/")
        entries: list[dict] = []
        for key in keys:
            val = self._bb.read(key)
            if isinstance(val, dict):
                entries.append(val)
        # Sort by timestamp descending, then by PR number descending as tiebreaker
        entries.sort(
            key=lambda e: (e.get("timestamp", ""), e.get("pr") or 0),
            reverse=True,
        )
        return entries[:limit]

    def stats(self) -> dict:
        """Return aggregate stats across all scored PRs.

        Entries with ``applicable: false`` are excluded from averages so that
        non-Python PRs (markdown, shell, config) don't drag down or distort
        the reported mean score.
        """
        all_scores = self.history(limit=10000)
        if not all_scores:
            return {
                "total_scored": 0,
                "avg_total": None,
                "avg_complexity": None,
                "avg_test_coverage": None,
                "avg_review_rounds": None,
                "avg_size": None,
                "grade_distribution": {},
            }

        # Only include applicable entries in averages
        scorable = [e for e in all_scores if e.get("applicable", True) is not False]
        n = len(scorable)
        grade_dist: dict[str, int] = {}

        if n == 0:
            return {
                "total_scored": len(all_scores),
                "avg_total": None,
                "avg_complexity": None,
                "avg_test_coverage": None,
                "avg_review_rounds": None,
                "avg_size": None,
                "grade_distribution": {},
            }

        totals = {"total": 0, "complexity": 0, "test_coverage": 0, "review_rounds": 0, "size": 0}
        for entry in scorable:
            totals["total"] += entry.get("total_score") or 0
            bd = entry.get("breakdown", {})
            totals["complexity"] += bd.get("complexity", {}).get("score", 0)
            totals["test_coverage"] += bd.get("test_coverage", {}).get("score", 0)
            totals["review_rounds"] += bd.get("review_rounds", {}).get("score", 0)
            totals["size"] += bd.get("size", {}).get("score", 0)
            grade = entry.get("grade", "F")
            grade_dist[grade] = grade_dist.get(grade, 0) + 1

        return {
            "total_scored": len(all_scores),
            "avg_total": round(totals["total"] / n, 1),
            "avg_complexity": round(totals["complexity"] / n, 1),
            "avg_test_coverage": round(totals["test_coverage"] / n, 1),
            "avg_review_rounds": round(totals["review_rounds"] / n, 1),
            "avg_size": round(totals["size"] / n, 1),
            "grade_distribution": grade_dist,
        }

    # ------------------------------------------------------------------
    # Scoring dimensions
    # ------------------------------------------------------------------

    def _complexity_score(self, files: dict[str, list[str]]) -> dict:
        """Score complexity of changed Python files using per-function McCabe average (max 30 pts).

        Formula: 30 * (1 - min(avg / 8, 1))
        Threshold at 8 — mainstream McCabe guideline says 1-10 is simple, 10+ gets hairy.
        Pure pydantic/dataclass class bodies are excluded from function collection.
        """
        py_files = {f: lines for f, lines in files.items() if f.endswith(".py")}

        if not py_files:
            # No Python files — full marks (nothing to penalise)
            return {"score": 30, "max": 30, "measured": True, "detail": "no Python files changed"}

        all_func_complexities: list[int] = []
        for filename, added_lines in py_files.items():
            # Try to read the file from working tree first; fall back to added lines
            full_path = self._repo_root / filename
            if full_path.exists():
                try:
                    source = full_path.read_text(encoding="utf-8", errors="ignore")
                    all_func_complexities.extend(_file_complexities(source))
                    continue
                except OSError:
                    pass
            # Fall back to added lines only
            all_func_complexities.extend(_file_complexities("\n".join(added_lines)))

        if not all_func_complexities:
            return {
                "score": 30,
                "max": 30,
                "measured": True,
                "detail": "avg_func_complexity=0.0 (0 functions)",
            }

        avg = sum(all_func_complexities) / len(all_func_complexities)
        score = round(30 * (1 - min(avg / 8, 1)))
        return {
            "score": score,
            "max": 30,
            "measured": True,
            "detail": f"avg_func_complexity={avg:.1f} ({len(all_func_complexities)} functions)",
        }

    def _test_coverage_score(self, files: dict[str, list[str]]) -> dict:
        """Score test file presence for changed modules (max 25 points).

        A module is credited when a *tracked* test file exists anywhere in
        the repo (per `git ls-files` — never a filesystem walk, so an
        untracked scratch file cannot manufacture coverage) whose basename
        matches ``^test_<norm>([._].*)?\\.(py|sh)$``, where ``<norm>`` is the
        module's basename stem with ``-`` normalised to ``_``. Matching is
        deliberately NOT restricted to the module's own directory — measured
        against this repo, only 4.5% of tracked modules keep their test in
        the same directory, so a same-directory rule is a near-total miss.
        The pattern is boundary-anchored (not a prefix/startswith check) so
        e.g. `test_apply.py` cannot falsely credit a module named `app`.
        """
        # Find all changed module files (non-test .py files)
        module_files = [
            f for f in files
            if f.endswith(".py")
            and not Path(f).name.startswith("test_")
            and not Path(f).name.endswith("_test.py")
        ]

        if not module_files:
            return {"score": 25, "max": 25, "measured": True, "detail": "no modules changed"}

        if self._test_index is None:
            # git ls-files failed — we have no basis to say a module lacks a
            # test, so report unmeasured rather than scoring every module 0.
            return {
                "score": 25,
                "max": 25,
                "measured": False,
                "detail": "unmeasured: git ls-files failed, cannot build test index",
            }

        covered_modules: list[str] = []
        for mod_path in module_files:
            norm = Path(mod_path).stem.replace("-", "_")
            pattern = re.compile(rf"^test_{re.escape(norm)}([._].*)?\.(py|sh)$")
            if any(pattern.match(Path(t).name) for t in self._test_index):
                covered_modules.append(mod_path)

        total = len(module_files)
        covered = len(covered_modules)
        score = round(25 * (covered / total))
        return {
            "score": score,
            "max": 25,
            "measured": True,
            "detail": f"{covered}/{total} modules covered",
            "covered_modules": sorted(covered_modules),
        }

    def _review_rounds_score(self, pr_number: int | None) -> dict:
        """Score based on review round count (max 25 points).

        Reports measured=False — rather than silently awarding full marks —
        when there is no real signal to read: no PR number was given, or the
        `agent_output/` blackboard namespace (the only place needs-fix
        verdicts are recorded) holds zero records. A dimension that cannot
        be measured must say so; `score_diff` excludes unmeasured dimensions
        from the renormalised total so this can neither inflate nor deflate
        the PR's grade.
        """
        if pr_number is None:
            return {
                "score": 25,
                "max": 25,
                "measured": False,
                "detail": "unmeasured: no PR number provided",
            }

        # Count needs-fix verdicts from blackboard agent_output entries
        keys = self._bb.list_keys("agent_output/")
        if not keys:
            return {
                "score": 25,
                "max": 25,
                "measured": False,
                "detail": "unmeasured: agent_output/ blackboard namespace has no records",
            }

        rounds = self._count_needs_fix_rounds(pr_number, keys=keys)

        if rounds == 0:
            score = 25
        elif rounds == 1:
            score = 20
        elif rounds == 2:
            score = 15
        else:
            score = 5

        return {
            "score": score,
            "max": 25,
            "measured": True,
            "detail": f"{rounds} needs-fix round(s)",
        }

    def _size_score(self, lines: int) -> dict:
        """Score based on total lines changed (max 20 points)."""
        if lines < 100:
            score = 20
            label = f"{lines} lines changed"
        elif lines < 250:
            score = 15
            label = f"{lines} lines changed"
        elif lines < 500:
            score = 10
            label = f"{lines} lines changed"
        else:
            score = 5
            label = f"{lines} lines changed"

        return {"score": score, "max": 20, "measured": True, "detail": label}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_pr_head_sha(self, pr_number: int) -> str | None:
        """Return the current head commit SHA for *pr_number*, or None on failure."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "view", str(pr_number),
                    "--repo", CODE_REPO,
                    "--json", "headRefOid",
                    "--jq", ".headRefOid",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                sha = result.stdout.strip()
                return sha if sha else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _load_cached_score(
        self, pr_number: int, head_sha: str, ttl_sec: int
    ) -> dict | None:
        """Return cached blackboard score if it matches *head_sha* and is within TTL.

        Returns None on any mismatch — sha changed, entry missing, TTL exceeded,
        or the stored entry lacks a head_sha field.
        """
        stored = self._bb.read(f"quality/{pr_number}")
        if not isinstance(stored, dict):
            return None
        if stored.get("head_sha") != head_sha:
            return None
        # TTL check: compare stored timestamp against now.
        ts_str = stored.get("timestamp")
        if not ts_str:
            return None
        try:
            stored_at = datetime.fromisoformat(ts_str)
            age_sec = (datetime.now(timezone.utc) - stored_at).total_seconds()
            if age_sec > ttl_sec:
                return None
        except (ValueError, TypeError):
            return None
        return stored

    def _fetch_pr_diff(self, pr_number: int) -> str:
        """Fetch the diff for a PR via gh CLI, falling back to empty string."""
        try:
            result = subprocess.run(
                ["gh", "pr", "diff", str(pr_number), "--repo", CODE_REPO],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return ""

    def _build_test_index(self) -> list[str] | None:
        """Return tracked repo file paths via `git ls-files`, or None on failure.

        Cached once on the instance in ``__init__`` — `score_diff` is called
        per PR and must not shell out to git repeatedly. Deliberately uses
        `git ls-files`, never a filesystem walk: an untracked scratch file
        must never be able to manufacture coverage. `archive/` is excluded
        so archived snapshots of old modules/tests can't skew matching.
        """
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None
            return [
                f for f in result.stdout.splitlines()
                if f and not f.startswith("archive/")
            ]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _count_needs_fix_rounds(self, pr_number: int, keys: list[str] | None = None) -> int:
        """Count how many needs-fix verdicts exist for this PR in the blackboard."""
        if keys is None:
            keys = self._bb.list_keys("agent_output/")
        count = 0
        for key in keys:
            val = self._bb.read(key)
            if not isinstance(val, dict):
                continue
            # Match PR number and needs-fix verdict
            if val.get("pr") == pr_number and val.get("verdict") == "needs-fix":
                count += 1
        return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quality_scorer",
        description="Score PR code quality on a 0-100 scale.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    score_cmd = sub.add_parser("score", help="Score a PR or diff file")
    score_group = score_cmd.add_mutually_exclusive_group(required=True)
    score_group.add_argument("--pr", type=int, metavar="N", help="PR number to score")
    score_group.add_argument("--diff", metavar="FILE", help="Path to a diff file")
    score_cmd.add_argument("--discussion", type=int, default=None)
    score_cmd.add_argument(
        "--cache-ttl-sec",
        type=int,
        default=300,
        metavar="N",
        help=(
            "Return the cached blackboard result when the PR head SHA matches "
            "and the score is younger than N seconds (default 300). "
            "Pass 0 to force a full recompute."
        ),
    )
    score_cmd.add_argument(
        "--quiet-if-not-applicable",
        action="store_true",
        default=False,
        help="Exit 2 silently when applicable=false (no scorable files); useful for CI gates",
    )

    sub.add_parser("history", help="Show scores for the last 20 PRs")
    sub.add_parser("stats", help="Show aggregate stats across all scored PRs")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    scorer = QualityScorer()

    if args.command == "score":
        cache_ttl = getattr(args, "cache_ttl_sec", 300)
        if args.pr is not None:
            result = scorer.score_pr(args.pr, discussion=args.discussion, cache_ttl_sec=cache_ttl)
        else:
            diff_path = Path(args.diff)
            if not diff_path.exists():
                print(f"error: diff file not found: {args.diff}", file=sys.stderr)
                return 1
            diff_text = diff_path.read_text(encoding="utf-8")
            result = scorer.score_diff(diff_text, discussion=args.discussion)
        # --quiet-if-not-applicable: exit 2 silently when no scorable files
        if getattr(args, "quiet_if_not_applicable", False) and not result.get("applicable", True):
            return 2
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "history":
        entries = scorer.history()
        if not entries:
            print("No scored PRs found.")
            return 0
        print(f"{'PR':<6} {'Grade':<6} {'Score':<8} {'Timestamp'}")
        print("-" * 50)
        for e in entries:
            pr_str = str(e.get("pr") or "—")
            print(
                f"{pr_str:<6} {e.get('grade', '?'):<6} "
                f"{e.get('total_score', 0):<8} {e.get('timestamp', '?')}"
            )
        return 0

    if args.command == "stats":
        data = scorer.stats()
        print(json.dumps(data, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
