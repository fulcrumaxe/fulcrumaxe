"""backend/stats/review_findings.py — D#2426: report the review-findings
coverage gap, honestly, instead of a ratio the stored data cannot support.

Why this module exists
-----------------------
D#2426 originally asked "what fraction of non-blocking reviewer findings get
acted on?". Measuring that turned up two facts that make the ratio
uncomputable from data we hold:

1. ``agent_run`` (the DuckDB table every agent spawn writes a row to) has no
   ``issues`` column and no ``severity`` column. The envelope's findings are
   dropped at ingest — see ``backend/agent_run_tracker.py::_ensure_schema``
   for the full column list.
2. ``.claude/agents/code-reviewer.md`` only requires ``issues`` to be
   populated when the verdict is ``needs-fix`` *and* the item is blocking.
   A non-blocking finding on a passing review has no required home in the
   envelope at all — the exact population this Discussion wants to count.

So this module does not compute a ratio. It reports, honestly:

* Whether ``agent_run.issues`` exists at all, and if so, how many
  non-blocking findings it holds (never a bare 0 when the column is simply
  absent — see ``issues_field_report``).
* Per-role verdict coverage: how many ``agent_run`` rows carry a terminal
  verdict (``pass``/``needs-fix``) out of the total, so the reader can judge
  how much of the table is even usable.
* Findings recovered from the one place non-blocking notes reliably land:
  trusted PR review comments on the code plane (see
  ``.claude/agents/code-reviewer.md`` step 6 — "Code review issues: ..." and
  the pass-path "brief note if any suggestions"). Every finding is labelled
  ``linked`` or ``unlinked`` based only on data actually supplied to this
  module — it does no inference of its own about whether a finding became
  work.

Read-only discipline
---------------------
This module never opens a DuckDB connection itself — every function here
takes an already-open connection or already-fetched comment data. Callers
are responsible for opening ``read_only=True`` (see
``backend/stats_connection.py::get_read_connection``) and for never writing
under ``AUTONOMOUS_TEAM_STATE_DIR``. Nothing in this module writes a file,
mutates a row, or touches state (matches the "read-only: never writes files,
never modifies state" discipline of ``backend/stats/analyst_findings.py``).

Absent vs. zero, everywhere in this module
--------------------------------------------
The one property every function here is built around: a field, a column, or
a corpus that was never measured must never render the same as one that was
measured and came back empty. See ``issues_field_report`` (``None`` vs
``0``) and ``corpus_report`` (``"unmeasured"`` vs ``"no_data"`` vs
``"measured"``). The same property recurs one layer below ``findings``:
``corpus_report``'s ``comments_recognized`` distinguishes "read N comments,
none of them matched the code-review marker" from "read N comments, all
recognized, none held a finding" — see ``is_code_reviewer_comment``'s
docstring for why the marker match was broadened to catch this.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# code-reviewer.md:257 conditions `issues` on verdict == needs-fix AND the
# item being blocking. A non-blocking finding on a pass review has no
# required home — that's the coverage gap. "Terminal" here means the row
# reached one of the two real outcomes a review can have, as opposed to
# null/reconciled-stale/unknown/etc.
TERMINAL_VERDICTS: tuple[str, ...] = ("pass", "needs-fix")

# code-reviewer.md's own severity vocabulary: error | warning | suggestion.
# "error" is blocking; the other two are the population this Discussion is
# about.
NON_BLOCKING_SEVERITIES: tuple[str, ...] = ("suggestion", "warning")

# code-reviewer.md:119/127's own template says "Code review passed." and
# "Code review issues:" verbatim — but the live corpus (trusted PR #114
# comments, 2026-09-10) shows the agent actually posts "Code review: needs-fix,
# on CI grounds." and "Code review: passed.", which the old literal-prefix
# check (two exact strings) silently failed to recognize: a real 3-comment
# corpus round-tripped through corpus_report() as comments_read=3, findings=0
# — indistinguishable from a corpus that genuinely has no findings, which is
# the exact defect class this module exists to make visible. Match on the
# stable two-word opening instead of the punctuation that follows it, so
# "issues:", "passed", ": needs-fix", ": passed." and any other verdict
# phrasing the agent settles on all recognize. A comment that doesn't start
# this way is not a code-review finding comment — it might be a debater note,
# an acceptance-tester note, or an outside comment (already partitioned out by
# pr_comment_trust before this runs).
_CODE_REVIEWER_MARKER_RE = re.compile(r"^code review\b", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# agent_run: schema + verdict coverage
# ---------------------------------------------------------------------------

def agent_run_columns(conn: Any) -> set[str]:
    """Column names on the agent_run table, via information_schema.

    DuckDB has no ``PRAGMA table_info()`` (see the comment at
    ``backend/agent_run_tracker.py::_ensure_schema`` — it uses
    ``information_schema.columns`` for exactly this reason).
    """
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'"
    ).fetchall()
    return {r[0] for r in rows}


def verdict_coverage(conn: Any, role: str) -> dict[str, Any]:
    """Terminal-verdict rows vs. total rows for one role in agent_run.

    "Terminal" = verdict in ('pass', 'needs-fix'). Everything else (null,
    reconciled-stale, unknown, swept-test-fixture, superseded, done, fail,
    ...) is not a terminal review outcome and is excluded from the numerator
    but included in the denominator, so the ratio reads as "how much of this
    table can even answer the question."
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM agent_run WHERE role = ?", [role]
    ).fetchone()[0]
    placeholders = ",".join("?" for _ in TERMINAL_VERDICTS)
    terminal = conn.execute(
        f"SELECT COUNT(*) FROM agent_run WHERE role = ? AND verdict IN ({placeholders})",
        [role, *TERMINAL_VERDICTS],
    ).fetchone()[0]
    return {"role": role, "terminal": terminal, "total": total}


def issues_field_report(conn: Any) -> dict[str, Any]:
    """Whether agent_run.issues exists, and if so, how many non-blocking
    findings it holds.

    Returns ``non_blocking_count: None`` when the column is absent — never
    0. A reader that returned 0 for "column missing" would read identically
    to "column present, nothing found", which is precisely the defect class
    D#2426 is about. ``issues_column_present`` is the field that lets a
    caller tell the two apart without parsing the count.
    """
    cols = agent_run_columns(conn)
    if "issues" not in cols:
        return {"issues_column_present": False, "non_blocking_count": None}

    rows = conn.execute("SELECT issues FROM agent_run WHERE issues IS NOT NULL").fetchall()
    count = 0
    for (raw,) in rows:
        items = raw
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except json.JSONDecodeError:
                continue
        if not isinstance(items, list):
            continue
        for item in items:
            sev = item.get("severity") if isinstance(item, dict) else None
            if sev in NON_BLOCKING_SEVERITIES:
                count += 1
    return {"issues_column_present": True, "non_blocking_count": count}


# ---------------------------------------------------------------------------
# PR-comment corpus: the one place non-blocking notes reliably land
# ---------------------------------------------------------------------------

def is_code_reviewer_comment(body: str) -> bool:
    """True when *body* opens the way code-reviewer.md's posted comments do,
    template or live phrasing alike (see ``_CODE_REVIEWER_MARKER_RE``)."""
    stripped = (body or "").lstrip()
    return bool(_CODE_REVIEWER_MARKER_RE.match(stripped))


def classify_linkage(finding: dict[str, Any]) -> str:
    """'linked' only when the finding carries a non-empty downstream_ref.

    This module does no inference: it does not scan commit messages, diffs,
    or follow-up comments looking for a match. A caller that has evidence a
    finding became work attaches it as ``downstream_ref`` (e.g. a commit SHA
    or a follow-up comment URL); absent that, the finding is reported
    unlinked — a fact about what we can show, not a claim about what
    happened to it.
    """
    ref = finding.get("downstream_ref")
    return "linked" if ref else "unlinked"


def extract_findings(comments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull individual finding lines out of trusted code-reviewer comments.

    *comments* is the TRUSTED half of a partition produced by
    ``scripts/lib/pr_comment_trust.py`` — never call this on unfiltered PR
    comments. Each returned finding carries ``text``, ``source_url``, and
    (if present on the source comment) ``downstream_ref`` for
    ``classify_linkage`` to read.
    """
    out: list[dict[str, Any]] = []
    for comment in comments:
        body = comment.get("body", "") or ""
        if not is_code_reviewer_comment(body):
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped[:1] in ("-", "*"):
                text = stripped[1:].strip()
            elif stripped[:1].isdigit() and "." in stripped[:4]:
                text = stripped.split(".", 1)[1].strip()
            else:
                continue
            if not text:
                continue
            finding: dict[str, Any] = {
                "text": text,
                "source_url": comment.get("url", ""),
            }
            if comment.get("downstream_ref"):
                finding["downstream_ref"] = comment["downstream_ref"]
            out.append(finding)
    return out


def corpus_report(
    comments: Optional[list[dict[str, Any]]],
    findings: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Summarize the PR-comment corpus with three distinguishable states.

    * ``comments is None``      -> "unmeasured": this run never fetched the
      corpus at all. Absent, not zero.
    * ``comments == []``        -> "no_data": the corpus was fetched and
      genuinely has nothing in it. Measured zero, not absent.
    * ``comments`` non-empty    -> "measured": findings (if not supplied,
      extracted from *comments* directly) are reported with linkage.

    ``comments_recognized`` carries the same absent-vs-zero discipline as
    everything else in this module, one layer below ``findings``: it counts
    how many of the *comments* actually matched ``is_code_reviewer_comment``,
    so a recognition failure (every comment read, none of them recognized as
    a code-review comment) is visible as ``comments_recognized: 0`` rather
    than silently reading identically to a genuinely findings-free corpus.
    Without it, ``findings: []`` cannot be told apart from "the marker check
    didn't recognize any of these comments" — which is exactly how the old
    two-literal-string check failed against the real PR #114 corpus.
    """
    if comments is None:
        return {
            "status": "unmeasured",
            "comments_read": None,
            "comments_recognized": None,
            "findings": None,
        }
    if not comments:
        return {
            "status": "no_data",
            "comments_read": 0,
            "comments_recognized": 0,
            "findings": [],
        }
    recognized = sum(1 for c in comments if is_code_reviewer_comment(c.get("body", "") or ""))
    found = findings if findings is not None else extract_findings(comments)
    labelled = [dict(f, linkage=classify_linkage(f)) for f in found]
    return {
        "status": "measured",
        "comments_read": len(comments),
        "comments_recognized": recognized,
        "findings": labelled,
    }


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def build_report(
    conn: Any,
    *,
    scope: str,
    host: str,
    roles: tuple[str, ...] = ("code-reviewer", "security-reviewer"),
    pr_comments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Assemble the full coverage-gap report.

    *conn* must already be open (read-only) and point at the ``agent_run``
    table. *scope* and *host* are carried on the report and printed next to
    every number in ``render_report`` — see the module docstring on the
    "absent vs. zero" property and D#2426 acceptance item 6 ("scope and host
    on every count").
    """
    return {
        "scope": scope,
        "host": host,
        "generated_at": _now_iso(),
        "coverage": {role: verdict_coverage(conn, role) for role in roles},
        "issues_field": issues_field_report(conn),
        "corpus": corpus_report(pr_comments),
    }


def _with_provenance(line: str, *, scope: str, host: str) -> str:
    """Append '(scope=..., host=...)' to any line carrying a digit, unless
    it already names its scope. Acceptance item 6: a bare integer anywhere
    in the rendered output fails the item — this is what keeps every count
    tied to where and on what it was measured."""
    if any(ch.isdigit() for ch in line) and "scope=" not in line:
        return f"{line} (scope={scope}, host={host})"
    return line


def render_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of a report built by ``build_report``."""
    scope = report["scope"]
    host = report["host"]

    lines: list[str] = [
        f"Review-findings coverage report — generated {report['generated_at']}",
        f"scope={scope} host={host}",
        "",
        "== Verdict coverage (agent_run) ==",
    ]
    for role, cov in report["coverage"].items():
        lines.append(
            f"{role}: {cov['terminal']}/{cov['total']} rows carry a terminal verdict (pass/needs-fix)."
        )

    lines += ["", "== issues column (agent_run) =="]
    issues = report["issues_field"]
    if not issues["issues_column_present"]:
        lines.append(
            "agent_run.issues: ABSENT — the envelope's findings are dropped at ingest; "
            "no finding count can be reported from this table. The verdict coverage above "
            "is everything agent_run can show for this question."
        )
    else:
        lines.append(
            f"agent_run.issues: PRESENT — {issues['non_blocking_count']} non-blocking "
            "findings recorded."
        )

    lines += ["", "== PR-comment corpus =="]
    corpus = report["corpus"]
    if corpus["status"] == "unmeasured":
        lines.append(
            "not fetched this run — no PR comments were requested, so no findings can be "
            "reported (absent, not zero)."
        )
    elif corpus["status"] == "no_data":
        lines.append("no data available — comments read 0/0, coverage 0.")
    else:
        lines.append(
            f"{corpus['comments_read']} trusted comments read; "
            f"{corpus['comments_recognized']} recognized as code-review comments; "
            f"{len(corpus['findings'])} findings extracted."
        )
        if corpus["comments_recognized"] < corpus["comments_read"]:
            unrecognized = corpus["comments_read"] - corpus["comments_recognized"]
            lines.append(
                f"  {unrecognized} trusted comment(s) did not match the code-review marker "
                "and were skipped — a recognition gap, not evidence those comments held no "
                "findings."
            )
        for finding in corpus["findings"]:
            lines.append(f"  [{finding['linkage']}] {finding['text']}")

    return "\n".join(_with_provenance(line, scope=scope, host=host) for line in lines)
