#!/usr/bin/env python3
"""Find call sites that resolve one repo plane and spend the slug on the other.

Background
----------
Code, PRs, CI and PR labels live on the **code plane**; Discussions, Issues and
intake live on the **Discussion plane**. Today ``code_repo`` is unset, so both
resolvers return the same slug and every mis-planed call site is correct *by
coincidence*. Setting ``code_repo`` ends the coincidence for all of them at the
same instant, which is why they have to be found and classified before the flip
rather than after.

Why this is a program and not a grep
------------------------------------
Three line-based sweeps produced confidently wrong answers on this problem
before it was handed to a detector:

* A ``grep`` for ``gh pr .* --repo .* _resolve_repo``-shaped lines missed
  ``scripts/lib/security-trigger.sh`` and ``scripts/lib/stuck-pr-detect.sh``,
  because both split the invocation across a backslash continuation. One of the
  two is the security gate.
* A per-file classification called ``scripts/bootstrap-github-labels.sh``
  "correct", when it is correct at one call site and wrong at another.
* A sweep written for bash reported a total for a codebase where the Python
  surface — modules importing the undifferentiated ``REPO`` from
  ``backend._repo`` and spending it on ``gh pr`` — had never been measured at
  all.

So this detector is:

* **multi-line aware** in bash — it joins backslash continuations, spans
  unbalanced quotes, and skips heredoc bodies, so an invocation is analysed as
  one logical line however it is spelled across the file;
* **language aware** — the Python surface is walked with ``ast``, which is
  immune to line breaks by construction rather than by effort;
* **per call site**, never per file;
* **self-validating** — ``--self-test`` runs the scanners over fixtures whose
  shapes are copied verbatim from known real defects and *fails* if any of them
  goes unflagged. A clean result from an unvalidated detector is
  indistinguishable from a broken detector.

The fixtures, not the live tree, are what ``--self-test`` checks. That is
deliberate: validating against live known-positives self-destructs the moment
those positives are fixed, which is the same run that most needs the guarantee.

Usage
-----
    python3 scripts/audit_repo_plane.py                # inventory + defects
    python3 scripts/audit_repo_plane.py --check        # exit 1 on a REGRESSION
    python3 scripts/audit_repo_plane.py --strict       # exit 1 if ANY defect
    python3 scripts/audit_repo_plane.py --self-test    # validate the detector
    python3 scripts/audit_repo_plane.py --json         # machine-readable
    python3 scripts/audit_repo_plane.py --cleared      # the cleared set + why

``--check`` is a ratchet, not a zero-defect gate: it fails when the tree gets
worse than the recorded ledger, and passes while the recorded defects are
merely still there. It was previously documented as "exit 1 if any defect
remains" while exiting 0 with 36 recorded, which described neither what it did
nor what it should do. ``--strict`` is the zero-defect gate for callers who
want one.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees that are not live code. archive/ is retained deliberately (Archive
# Protocol) but a snapshot of a script is not a call site anyone can reach.
SKIP_DIRS = {
    ".git",
    "archive",
    "loop-bootstrap",
    "node_modules",
    "__pycache__",
    ".claude",
    "open-source",
}

# --------------------------------------------------------------------------
# Planes
# --------------------------------------------------------------------------
CODE = "code"
DISCUSSION = "discussion"
# _resolve_repo / backend._repo.REPO name neither plane. Today they equal the
# code plane's value; after the flip they follow the Discussion plane, because
# both read the same "repo" key the Discussion plane falls back to. Spending
# one on a code-plane call is the defect this detector exists to find.
UNDIFFERENTIATED = "undifferentiated"
# The binding could not be established by this detector, on a surface where
# getting it wrong matters. Distinct from "unknown" (a function parameter, whose
# plane is the caller's choice and which a caller trace settles): UNRESOLVED
# means the identifier lives in another language's scope inside an embedded
# string, so matching it against this file's variables is not evidence at all.
# Treated as a defect on a code surface — unproven fails closed, the same rule
# the security trigger and the ledger loader follow.
UNRESOLVED = "unresolved"

BASH_RESOLVERS = {
    # The guarded accessor, which is what a fixed call site should use: it
    # returns the code plane or aborts, never an empty string.
    "_require_code_repo": CODE,
    "_resolve_code_repo": CODE,
    "_resolve_discussion_repo": DISCUSSION,
    "_resolve_repo": UNDIFFERENTIATED,
}

PY_RESOLVERS = {
    "CODE_REPO": CODE,
    "DISCUSSION_REPO": DISCUSSION,
    "REPO": UNDIFFERENTIATED,
}

# --------------------------------------------------------------------------
# Surface classification — which plane a `gh` invocation actually talks to
# --------------------------------------------------------------------------
SURFACE_CODE = "CODE"
SURFACE_DISCUSSION = "DISCUSSION"
SURFACE_AMBIGUOUS = "AMBIGUOUS"

# Ordered: first match wins.
_SURFACE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # --- unambiguously code plane -----------------------------------------
    ("gh pr", re.compile(r"^gh\s+pr\b"), SURFACE_CODE),
    ("gh run", re.compile(r"^gh\s+run\b"), SURFACE_CODE),
    ("gh workflow", re.compile(r"^gh\s+workflow\b"), SURFACE_CODE),
    ("gh release", re.compile(r"^gh\s+release\b"), SURFACE_CODE),
    ("gh search prs", re.compile(r"^gh\s+search\s+prs\b"), SURFACE_CODE),
    # Actions variables/secrets/caches are CI state. `gh variable set
    # CI_DISABLED` is the kill switch; the gate that honours it reads the same
    # variable off the code plane, so a write on any other plane is a
    # split-brain kill switch that cannot be read.
    ("gh variable", re.compile(r"^gh\s+variable\b"), SURFACE_CODE),
    ("gh secret", re.compile(r"^gh\s+secret\b"), SURFACE_CODE),
    ("gh cache", re.compile(r"^gh\s+cache\b"), SURFACE_CODE),
    ("api /environments", re.compile(r"repos/[^\s\"']*/environments\b"), SURFACE_CODE),
    ("api /pulls", re.compile(r"repos/[^\s\"']*/pulls\b"), SURFACE_CODE),
    ("api /commits", re.compile(r"repos/[^\s\"']*/commits\b"), SURFACE_CODE),
    ("api /statuses", re.compile(r"repos/[^\s\"']*/statuses\b"), SURFACE_CODE),
    ("api /check-runs", re.compile(r"repos/[^\s\"']*/check-runs\b"), SURFACE_CODE),
    ("api /actions", re.compile(r"repos/[^\s\"']*/actions\b"), SURFACE_CODE),
    ("api /compare", re.compile(r"repos/[^\s\"']*/compare\b"), SURFACE_CODE),
    ("api /branches", re.compile(r"repos/[^\s\"']*/branches\b"), SURFACE_CODE),
    ("graphql pullRequest", re.compile(r"\bpullRequests?\s*\("), SURFACE_CODE),
    # --- unambiguously Discussion plane -----------------------------------
    ("graphql discussion", re.compile(r"\bdiscussions?\s*\("), SURFACE_DISCUSSION),
    ("api /discussions", re.compile(r"repos/[^\s\"']*/discussions\b"), SURFACE_DISCUSSION),
    # --- needs a human --------------------------------------------------
    # `gh issue ...` and `repos/X/issues/N` address BOTH surfaces: GitHub
    # numbers PRs and Issues in one sequence, so `gh issue comment 2401` may be
    # a PR comment. `gh label` / `repos/X/labels` likewise: label *definitions*
    # must exist on whichever plane the labelled thing lives on. These are
    # reported, never auto-cleared.
    ("gh issue", re.compile(r"^gh\s+issue\b"), SURFACE_AMBIGUOUS),
    ("gh label", re.compile(r"^gh\s+label\b"), SURFACE_AMBIGUOUS),
    ("api /issues", re.compile(r"repos/[^\s\"']*/issues\b"), SURFACE_AMBIGUOUS),
    ("api /labels", re.compile(r"repos/[^\s\"']*/labels\b"), SURFACE_AMBIGUOUS),
]


def classify_surface(invocation: str) -> tuple[str, str]:
    """Return ``(surface, rule_name)`` for a normalised `gh` invocation."""
    for name, pattern, surface in _SURFACE_RULES:
        if pattern.search(invocation):
            return surface, name
    return SURFACE_AMBIGUOUS, "unclassified"



def _rel(path: Path, root: Path | None) -> str:
    """Path relative to the tree being scanned, not to this checkout.

    --root was accepted but never honoured here: every scanner computed its
    relative path against the module-level REPO_ROOT, so pointing the detector
    at another tree (a base commit, say) raised ValueError on the first file.
    A flag that cannot do the one thing it exists for is worse than no flag.
    """
    base = root or REPO_ROOT
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------
@dataclass
class Finding:
    path: str
    line: int
    language: str
    kind: str  # gh_call | gh_repo_export | source_time_leak
    surface: str
    rule: str
    binding: str  # variable name, "UNPINNED", or "LITERAL"
    plane: str  # plane the binding carries
    snippet: str

    @property
    def is_defect(self) -> bool:
        if self.kind == "gh_repo_export":
            return self.plane != CODE
        if self.kind == "source_time_leak":
            return True
        if self.surface != SURFACE_CODE:
            return False
        # UNRESOLVED counts. A code-plane call whose repo this detector cannot
        # establish is not evidence of correctness, and clearing it is how a
        # live misroute gets reported clean.
        return self.plane in (UNDIFFERENTIATED, DISCUSSION, UNRESOLVED)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "language": self.language,
            "kind": self.kind,
            "surface": self.surface,
            "rule": self.rule,
            "binding": self.binding,
            "plane": self.plane,
            "defect": self.is_defect,
            "snippet": self.snippet,
        }


# --------------------------------------------------------------------------
# Bash: logical lines
# --------------------------------------------------------------------------
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _scan_quote_state(text: str, state: str | None) -> str | None:
    """Advance the single/double quote state across one physical line.

    Returns the quote state at end of line: ``None``, ``"'"`` or ``'"'``.
    A ``#`` outside quotes starts a comment and ends the scan for that line.
    """
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if state is None:
            if c == "\\":
                i += 2
                continue
            if c == "#" and (i == 0 or text[i - 1].isspace()):
                return None
            if c in ("'", '"'):
                state = c
        elif state == "'":
            if c == "'":
                state = None
        else:  # inside a double quote
            if c == "\\":
                i += 2
                continue
            if c == '"':
                state = None
        i += 1
    return state


def _ends_with_continuation(text: str) -> bool:
    """True when the line ends in an odd number of backslashes."""
    stripped = text.rstrip("\n")
    n = len(stripped) - len(stripped.rstrip("\\"))
    return n % 2 == 1


def bash_logical_lines(source: str) -> list[tuple[int, str]]:
    """Join a bash source into ``(first_physical_line, logical_text)`` pairs.

    Joins across backslash continuations *and* across unbalanced quotes, and
    skips heredoc bodies entirely. The unbalanced-quote rule is what makes a
    multi-line ``gh api graphql -f query='...'`` analysable as one unit, and
    the continuation rule is what would have caught ``security-trigger.sh``.
    """
    lines = source.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        start = i + 1
        buf: list[str] = []
        state: str | None = None
        heredoc_terms: list[str] = []
        while i < len(lines):
            raw = lines[i]
            buf.append(raw.strip() if buf else raw)
            if state is None and not heredoc_terms:
                for m in _HEREDOC_RE.finditer(raw):
                    heredoc_terms.append(m.group(2))
            state = _scan_quote_state(raw, state)
            cont = _ends_with_continuation(raw)
            i += 1
            # Consume any heredoc bodies opened on this logical line.
            while heredoc_terms and i < len(lines):
                term = heredoc_terms[0]
                if lines[i].strip() == term:
                    heredoc_terms.pop(0)
                i += 1
            if state is None and not cont and not heredoc_terms:
                break
        # Preserve the first physical line's indentation: it is the only
        # signal distinguishing a source-time assignment from one inside a
        # function body, and joining must not destroy it.
        first = buf[0].rstrip("\\").rstrip()
        indent = first[: len(first) - len(first.lstrip())]
        parts = [first.strip()] + [p.rstrip("\\").strip() for p in buf[1:]]
        out.append((start, indent + " ".join(p for p in parts if p)))
    return out


# --------------------------------------------------------------------------
# Bash: taint + call extraction
# --------------------------------------------------------------------------
_ASSIGN_RE = re.compile(
    r"(?:^|;|\bthen\b|\bdo\b|\belse\b|&&|\|\|)\s*"
    r"(?:export\s+|local\s+|declare\s+-\w+\s+|readonly\s+)*"
    r"([A-Za-z_][A-Za-z0-9_]*)="
)
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_REPO_FLAG_RE = re.compile(r"(?:--repo|-R)[=\s]+[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_REPO_FLAG_LITERAL_RE = re.compile(r"(?:--repo|-R)[=\s]+[\"']?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_REPOS_PATH_RE = re.compile(r"repos/\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# A GraphQL query pins its repo with owner:/name:, not --repo. Missing this was
# a real blind spot in this detector's first draft: scripts/start-the-day.sh,
# scripts/auto-plan.sh and scripts/team-lead-iteration.sh all derive
# REPO_OWNER/REPO_NAME from the same REPO with ${REPO%%/*} and spend them on
# Discussion queries, and every one of those call sites was reported as
# "UNPINNED — inherits the process default", i.e. silently cleared. The
# quote-juggling spellings in the tree ( owner:\"$V\" and owner:"'"$V"'" ) both
# have to match, hence the permissive quote class.
_GRAPHQL_OWNER_RE = re.compile(
    r"owner\s*[:=]\s*[\\\"']*\$\{?([A-Za-z_][A-Za-z0-9_]*)"
)
_GH_CALL_RE = re.compile(r"(?<![\w./-])gh\s+(?=[a-z])")

# `gh` invoked from Python embedded in a bash string. The whole argv is a list
# of quoted literals, so nothing in it looks like a shell `gh ` token:
#
#   HTTP_RESPONSE=$(python3 -c "
#   result = subprocess.run(
#       ['gh', 'api', '-X', 'POST',
#        'repos/$_REPO/pulls', ...
#
# That is a real `gh` call against a bash-interpolated slug — the enclosing
# string is DOUBLE-quoted, so bash expands $_REPO before Python ever sees it —
# and the shell-token scanner is blind to every one of them.
_GH_PY_ARGV_RE = re.compile(r"""[\[\(]\s*(['"])gh\1\s*,""")

_DECL_HEAD_RE = re.compile(
    r"(?:^|;|\bthen\b|\bdo\b|\belse\b|&&|\|\|)\s*"
    r"((?:export|local|declare|readonly|typeset)(?:\s+-\w+)*)\s+"
)
_WORD_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _split_words(text: str) -> list[str]:
    """Split a shell fragment on whitespace, respecting quotes and $( ).

    Needed because a declaration can carry several assignments on one line:

        local pr="$1" repo="${2:-$(_resolve_repo)}"

    A regex that takes "everything after the first =" as the right-hand side
    attributes the resolver to `pr` and never records `repo` at all, which
    silently downgrades the resulting call site from a defect to "needs caller
    trace". Word boundaries are what make each RHS its own.
    """
    words: list[str] = []
    cur = ""
    i, n = 0, len(text)
    depth = 0
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote == "'":
            cur += c
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                cur += text[i:i + 2]
                i += 2
                continue
            cur += c
            if c == '"':
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur += text[i:i + 2]
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            cur += c
            i += 1
            continue
        if text.startswith("$(", i):
            depth += 1
            cur += "$("
            i += 2
            continue
        if c == "(" and depth:
            depth += 1
            cur += c
            i += 1
            continue
        if c == ")" and depth:
            depth -= 1
            cur += c
            i += 1
            continue
        if c.isspace() and not depth:
            if cur:
                words.append(cur)
                cur = ""
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        words.append(cur)
    return words


def _normalize_py_argv(text: str, start: int) -> str:
    """Turn a Python `['gh', 'api', ...]` argv into a shell-looking string.

    Rebuilding it as `gh api -X POST repos/$_REPO/pulls` lets every existing
    surface rule and repo-binding rule apply unchanged, instead of needing a
    parallel set for the embedded spelling.
    """
    depth = 0
    parts: list[str] = []
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c in "[(":
            depth += 1
        elif c in "])":
            depth -= 1
            if depth <= 0:
                break
        elif c in "'\"":
            j = text.find(c, i + 1)
            if j == -1:
                break
            parts.append(text[i + 1:j])
            i = j + 1
            continue
        i += 1
    return " ".join(parts)

# Source-time assignments in a sourced library execute in the CALLER's shell, so
# a name the caller also uses gets silently replaced. What makes that dangerous
# is the name being a common one, not merely the absence of a leading
# underscore: scripts/lib/stuck-pr-detect.sh's STUCK_PR_REPO is a documented
# public override and is namespaced perfectly well by its prefix, whereas
# gh-label.sh's plain REPO collided with scripts/sweep-stuck-prs.sh's own REPO
# and silently won. So flag the collision-prone names rather than every name
# without an underscore — an over-broad rule here trains people to ignore it.
_COLLISION_PRONE_NAMES = {
    "REPO", "OWNER", "NAME", "PR", "LOG", "DIR", "FILE", "OUT", "TMP", "URL",
    "repo", "owner", "name", "pr", "log", "dir", "file", "out", "tmp", "url",
}


def _rhs_plane(rhs: str, tainted: dict[str, str]) -> str | None:
    """The plane a right-hand side carries, or None if it carries no slug."""
    for fn, plane in BASH_RESOLVERS.items():
        if re.search(rf"{fn}(?![A-Za-z0-9_])", rhs):
            return plane
    for var in _VAR_REF_RE.findall(rhs):
        if var in tainted:
            return tainted[var]
    return None


def scan_bash(path: Path, source: str, root: Path | None = None) -> list[Finding]:
    rel = _rel(path, root)
    findings: list[Finding] = []
    tainted: dict[str, str] = {}
    in_lib = rel.startswith("scripts/lib/")

    for lineno, logical in bash_logical_lines(source):
        stripped = logical.lstrip()
        is_comment = stripped.startswith("#")

        # --- taint propagation --------------------------------------------
        if not is_comment:
            # Declaration statements first, word by word, so that every
            # assignment on a `local a=… b=…` line is seen and each gets its
            # own right-hand side. _ASSIGN_RE below then skips these names.
            handled: set[str] = set()
            decls: list[tuple[str, str, bool]] = []
            for dm in _DECL_HEAD_RE.finditer(logical):
                exported = dm.group(1).split()[0] == "export"
                for word in _split_words(logical[dm.end():]):
                    wm = _WORD_ASSIGN_RE.match(word)
                    if not wm:
                        break  # first non-assignment word is the command
                    handled.add(wm.group(1))
                    decls.append((wm.group(1), wm.group(2), exported))

            for name, rhs, exported in decls:
                plane = _rhs_plane(rhs, tainted)
                if plane is None:
                    continue
                tainted[name] = plane
                if name == "GH_REPO" and exported:
                    findings.append(
                        Finding(
                            path=rel, line=lineno, language="bash",
                            kind="gh_repo_export", surface=SURFACE_CODE,
                            rule="process-wide gh default", binding="GH_REPO",
                            plane=plane, snippet=logical.strip()[:200],
                        )
                    )

            for m in _ASSIGN_RE.finditer(logical):
                name = m.group(1)
                if name in handled:
                    continue
                rhs = logical[m.end():]
                plane = _rhs_plane(rhs, tainted)
                if plane is None:
                    continue
                tainted[name] = plane

                if name == "GH_REPO" and "export" in logical[: m.end()]:
                    findings.append(
                        Finding(
                            path=rel,
                            line=lineno,
                            language="bash",
                            kind="gh_repo_export",
                            surface=SURFACE_CODE,
                            rule="process-wide gh default",
                            binding="GH_REPO",
                            plane=plane,
                            snippet=logical.strip()[:200],
                        )
                    )
                # A sourced library that assigns an un-namespaced global at
                # source time silently overwrites the caller's own variable of
                # that name. scripts/lib/gh-label.sh:REPO did exactly this to
                # scripts/sweep-stuck-prs.sh.
                elif (
                    in_lib
                    and name in _COLLISION_PRONE_NAMES
                    and _at_top_level(logical)
                ):
                    findings.append(
                        Finding(
                            path=rel,
                            line=lineno,
                            language="bash",
                            kind="source_time_leak",
                            surface=SURFACE_AMBIGUOUS,
                            rule="un-namespaced global assigned at source time",
                            binding=name,
                            plane=plane,
                            snippet=logical.strip()[:200],
                        )
                    )

        if is_comment:
            continue

        # --- gh invoked from Python embedded in a bash string ---------------
        for m in _GH_PY_ARGV_RE.finditer(logical):
            invocation = _normalize_py_argv(logical, m.start())
            if not invocation:
                continue
            surface, rule = classify_surface(invocation)
            binding, plane = _bash_binding(invocation, tainted)
            findings.append(
                Finding(
                    path=rel, line=lineno, language="bash", kind="gh_call",
                    surface=surface, rule=f"{rule} (python-in-bash)",
                    binding=binding, plane=plane, snippet=invocation[:200],
                )
            )

        # --- gh invocations -------------------------------------------------
        for m in _GH_CALL_RE.finditer(logical):
            invocation = logical[m.start():].strip()
            # Drop a leading `gh ` that is part of a quoted string in an echo.
            surface, rule = classify_surface(invocation)
            binding, plane = _bash_binding(invocation, tainted)
            findings.append(
                Finding(
                    path=rel,
                    line=lineno,
                    language="bash",
                    kind="gh_call",
                    surface=surface,
                    rule=rule,
                    binding=binding,
                    plane=plane,
                    snippet=invocation[:200],
                )
            )
    return findings


def _at_top_level(logical: str) -> bool:
    """Crude check that an assignment is not inside a function body.

    Source-time execution is what matters; an assignment inside a function only
    runs when the function is called. We treat an assignment with no leading
    indentation as top-level, which is how every case in this tree is written.
    """
    return not logical.startswith((" ", "\t"))


def _bash_binding(invocation: str, tainted: dict[str, str]) -> tuple[str, str]:
    for regex in (_REPO_FLAG_RE, _REPOS_PATH_RE, _GRAPHQL_OWNER_RE):
        m = regex.search(invocation)
        if m:
            var = m.group(1)
            return var, tainted.get(var, "unknown")
    if _REPO_FLAG_LITERAL_RE.search(invocation):
        return "LITERAL", CODE
    return "UNPINNED", "inherited"


# --------------------------------------------------------------------------
# Python: ast-based scan
# --------------------------------------------------------------------------
_PY_CODE_PATH_RE = re.compile(
    r"/(pulls|commits|statuses|check-runs|actions|compare|branches)\b"
)


def _py_string_parts(node: ast.AST) -> tuple[list[str], list[str]]:
    """Flatten an expression into (literal fragments, referenced Names)."""
    literals: list[str] = []
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            literals.append(sub.value)
        elif isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
    return literals, names


def scan_python(path: Path, source: str, root: Path | None = None) -> list[Finding]:
    rel = _rel(path, root)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Which plane-carrying names this module imported from backend._repo.
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("_repo"):
            for alias in node.names:
                if alias.name in PY_RESOLVERS:
                    imported[alias.asname or alias.name] = PY_RESOLVERS[alias.name]
    if not imported:
        return []

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        if not args:
            continue
        literals, names = _py_string_parts(ast.Module(body=[ast.Expr(a) for a in args],
                                                     type_ignores=[]))
        if not literals:
            continue
        # Is this a `gh` invocation at all?
        joined = " ".join(literals)
        argv_is_gh = any(lit == "gh" for lit in literals)
        path_is_gh_api = "gh" in literals or _PY_CODE_PATH_RE.search(joined)
        if not (argv_is_gh or (path_is_gh_api and "repos/" in joined)):
            continue

        planed = [n for n in names if n in imported]
        if not planed:
            continue
        binding = planed[0]
        plane = imported[binding]

        surface, rule = _py_surface(literals)
        findings.append(
            Finding(
                path=rel,
                line=node.lineno,
                language="python",
                kind="gh_call",
                surface=surface,
                rule=rule,
                binding=binding,
                plane=plane,
                snippet=" ".join(joined.split())[:200],
            )
        )
    return findings


def _py_surface(literals: list[str]) -> tuple[str, str]:
    tokens = [lit for lit in literals if lit]
    joined = " ".join(tokens)
    if "pr" in tokens and "gh" in tokens:
        return SURFACE_CODE, "gh pr"
    for word, name in (
        ("run", "gh run"),
        ("workflow", "gh workflow"),
        ("release", "gh release"),
    ):
        if "gh" in tokens and word in tokens:
            return SURFACE_CODE, name
    if _PY_CODE_PATH_RE.search(joined):
        return SURFACE_CODE, "api code path"
    if "pullRequest" in joined or "pullRequests" in joined:
        return SURFACE_CODE, "graphql pullRequest"
    if "discussion" in joined or "discussions" in joined:
        return SURFACE_DISCUSSION, "graphql discussion"
    if "issue" in tokens or "/issues" in joined:
        return SURFACE_AMBIGUOUS, "issue surface"
    return SURFACE_AMBIGUOUS, "unclassified"


# --------------------------------------------------------------------------
# TypeScript: ts-backend's own resolver
# --------------------------------------------------------------------------
# ts-backend/src/config/repo.ts exports resolveRepo() (undifferentiated) beside
# resolveCodeRepo()/resolveDiscussionRepo(). This surface went unmeasured
# entirely in the first two audits because both sweeps were written for bash
# and then extended to Python — nobody asked whether there was a third
# language, and there is: the loop's merge path is TypeScript.
TS_RESOLVERS = {
    "resolveCodeRepo": CODE,
    "resolveDiscussionRepo": DISCUSSION,
    "resolveRepo": UNDIFFERENTIATED,
}

# `spawnSync("gh", [...])` / `execFileSync("gh", [...])`, with the "gh" and the
# array routinely on different lines — so this matches across newlines rather
# than per line.
_TS_GH_CALL_RE = re.compile(
    r"""(?:spawnSync|spawn|execFileSync|execFile)\s*\(\s*["']gh["']\s*,\s*\[""",
    re.S,
)

# The other TypeScript spelling: the whole argv, `gh` included, as ONE array
# literal handed to a helper — `["gh", "pr", "view", pr, "--repo", repo]`.
# The regex above requires "gh" to sit OUTSIDE the bracket as spawnSync's first
# argument, so it saw none of these. That hid all 9 code-plane sites in
# ts-backend/src/spawn/post-merge-hook.ts, a file on identical footing to
# loop-phased-step5.ts, which was recorded.
#
# Note this is the same shape as the Python-in-bash blind spot, in a third
# language: argv-as-list is how every non-shell caller spells a command, and
# each language needed its own rule because this detector matches text rather
# than parsing call expressions. See the docstring's note on that limit.
_TS_GH_ARRAY_RE = re.compile(r"""\[\s*["']gh["']\s*,""")
_TS_ASSIGN_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
)


def _ts_array_elements(text: str, start: int) -> list[tuple[str, str]]:
    """Ordered ``("lit"|"ident", value)`` for the array literal at *start*.

    Order is what makes the ``--repo`` rule precise. Two flat lists (literals,
    identifiers) lose the interleaving, so the only question they can answer is
    "does the flag appear anywhere" — and keying on the flag's PRESENCE rather
    than its VALUE is the defect this ordering exists to close: `--repo`
    followed by an unresolved identifier was read as "pinned to a literal" and
    cleared.
    """
    depth = 0
    elements: list[tuple[str, str]] = []
    cur_ident = ""
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth <= 0:
                break
        elif c in "'\"`":
            if cur_ident:
                elements.append(("ident", cur_ident))
                cur_ident = ""
            j = i + 1
            buf = ""
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 2
                    continue
                buf += text[j]
                j += 1
            elements.append(("lit", buf))
            i = j + 1
            continue
        elif c.isalnum() or c in "_$":
            cur_ident += c
            i += 1
            continue
        if cur_ident:
            elements.append(("ident", cur_ident))
            cur_ident = ""
        i += 1
    if cur_ident:
        elements.append(("ident", cur_ident))
    return elements


def _ts_string_ranges(source: str) -> list[tuple[int, int]]:
    """Spans of every string / template literal, so a call site can be asked
    whether it lives inside one.

    A `['gh', …]` array inside a TS string is not TypeScript — it is another
    language's source being assembled for `python3 -c`. Its identifiers belong
    to that language's scope, so matching them against this file's `const`
    declarations is a coincidence of naming, not a dataflow fact.
    """
    ranges: list[tuple[int, int]] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in "'\"`":
            start = i
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == c:
                    break
                if c != "`" and source[i] == "\n":
                    break  # unterminated ordinary string; do not run away
                i += 1
            ranges.append((start, min(i, n - 1)))
            i += 1
            continue
        i += 1
    return ranges


def _in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= pos <= b for a, b in ranges)


_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _ts_binding(
    elements: list[tuple[str, str]], tainted: dict[str, str]
) -> tuple[str, str]:
    """What a TS `gh` argv pins its repo to, keyed on ``--repo``'s VALUE.

    The bug this replaces keyed on the flag's PRESENCE: any argv containing
    ``--repo`` whose identifiers the taint map did not resolve was labelled
    ``LITERAL`` and assumed to be the code plane — cleared. The embedded-string
    case was one way to reach that branch; an ordinary unresolved identifier
    (a function parameter, an import, a property access, anything the
    assignment regex does not match) is another. Same defect, one level up.

    So: find ``--repo`` and look at what follows it.

    * a literal shaped like ``owner/name``  -> genuinely pinned, LITERAL/code
    * an identifier the taint map knows     -> that identifier's plane
    * an identifier it does not know        -> NEEDS_CALLER_TRACE / UNRESOLVED
    * no ``--repo`` at all                  -> UNPINNED, inherits the default

    The literal branch is kept rather than folded into "unresolved" because a
    hardcoded ``owner/name`` really is pinned — the detector can see exactly
    which repo it names. Flagging it would be a false positive, and a rule that
    cannot tell a pinned call from an unresolvable one is not more careful, just
    noisier. (Measured: no TypeScript site in this tree reaches it today.)
    """
    for idx, (kind, value) in enumerate(elements):
        if kind != "lit" or value not in ("--repo", "-R"):
            continue
        if idx + 1 >= len(elements):
            break  # trailing flag with no value — treat as unresolvable below
        nxt_kind, nxt_value = elements[idx + 1]
        if nxt_kind == "lit":
            if _SLUG_RE.match(nxt_value):
                return "LITERAL", CODE
            # A literal that is not a slug (an empty string, a placeholder) is
            # not a pin either. `gh --repo ""` is the failure this whole
            # workstream exists to close, so it must never read as pinned.
            return "NEEDS_CALLER_TRACE", UNRESOLVED
        if nxt_value in tainted:
            return nxt_value, tainted[nxt_value]
        return "NEEDS_CALLER_TRACE", UNRESOLVED

    # No --repo. Fall back to any identifier the taint map knows, so a call
    # pinned some other way (a repos/<x>/ path) still reports its plane.
    planed = [v for kind, v in elements if kind == "ident" and v in tainted]
    if planed:
        return planed[0], tainted[planed[0]]
    return "UNPINNED", "inherited"


def scan_typescript(path: Path, source: str, root: Path | None = None) -> list[Finding]:
    rel = _rel(path, root)
    tainted: dict[str, str] = {}
    for m in _TS_ASSIGN_RE.finditer(source):
        name, rhs = m.group(1), m.group(2)
        for fn, plane in TS_RESOLVERS.items():
            if re.search(rf"\b{fn}\s*\(", rhs):
                tainted[name] = plane
                break
    # A file that never calls a plane resolver is skipped entirely. That is a
    # real limit, not a proof of safety: a `gh` call here could still take its
    # repo from an import or a parameter. It is left as-is because the ledger
    # is a per-file ratchet and such a file has no plane of its own to get
    # wrong — but a whole-program binding resolution (D#2406) would need to
    # revisit it rather than inherit the assumption.
    if not tainted:
        return []

    # Both spellings resolve to the position of the argv "[", deduped so a call
    # matching each rule is reported once.
    brackets: set[int] = set()
    for m in _TS_GH_CALL_RE.finditer(source):
        brackets.add(source.index("[", m.end() - 1))
    for m in _TS_GH_ARRAY_RE.finditer(source):
        brackets.add(m.start())

    string_ranges = _ts_string_ranges(source)

    findings: list[Finding] = []
    for bracket in sorted(brackets):
        elements = _ts_array_elements(source, bracket)
        literals = [v for kind, v in elements if kind == "lit"]
        idents = [v for kind, v in elements if kind == "ident"]
        if not literals:
            continue
        # Do not double the program name when "gh" is the array's first element.
        body = literals[1:] if literals and literals[0] == "gh" else literals
        invocation = "gh " + " ".join(body)
        surface, rule = classify_surface(invocation)
        # An argv array inside a string literal is another language's source.
        # Its identifiers are not this file's, so neither a name match nor the
        # absence of one tells us anything: reported unresolved, which fails
        # closed on a code surface.
        #
        # Both halves of this were measured, not theorised. Feeding the embedded
        # `repo` from a Discussion-plane variable while an unrelated `const
        # repo` held the code plane reported `plane=code defect=False` — a live
        # misroute reported clean. And renaming the embedded variable so it
        # matched nothing dropped it to `LITERAL`/code, also cleared, which
        # would have deleted three real ledger entries and told the operator to
        # lower the baseline.
        if _in_ranges(bracket, string_ranges):
            binding, plane = "NEEDS_CALLER_TRACE", UNRESOLVED
        else:
            binding, plane = _ts_binding(elements, tainted)
        findings.append(
            Finding(
                path=rel, line=source[:bracket].count("\n") + 1,
                language="typescript", kind="gh_call", surface=surface,
                rule=rule, binding=binding, plane=plane,
                snippet=" ".join(invocation.split())[:200],
            )
        )
    return findings


# --------------------------------------------------------------------------
# Corpus walk
# --------------------------------------------------------------------------
def iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix == ".sh":
            yield path, "bash"
        elif path.suffix == ".py":
            yield path, "python"
        elif path.suffix == ".ts" and not path.name.endswith(".d.ts"):
            yield path, "typescript"


def scan_tree(root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path, lang in iter_sources(root):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if lang == "bash":
            findings.extend(scan_bash(path, source, root))
        elif lang == "python":
            findings.extend(scan_python(path, source, root))
        else:
            findings.extend(scan_typescript(path, source, root))
    return findings


# --------------------------------------------------------------------------
# Self-test — fixtures copied from real defects, in both languages
# --------------------------------------------------------------------------
@dataclass
class Fixture:
    name: str
    provenance: str
    language: str
    source: str
    expect_kinds: set[str] = field(default_factory=set)
    # Where the fixture pretends to live. Some rules are position-sensitive:
    # the source-time-leak rule only applies to files under scripts/lib/,
    # because only a sourced library can overwrite its caller's variables.
    rel_path: str | None = None


FIXTURES: list[Fixture] = [
    Fixture(
        name="gh_repo_process_wide_export",
        provenance="scripts/env-bootstrap.sh:19 before this change",
        language="bash",
        source=(
            'source "$_SCRIPT_DIR/lib/repo-resolve.sh"\n'
            'export GH_REPO="$(_resolve_repo)"\n'
        ),
        expect_kinds={"gh_repo_export"},
    ),
    Fixture(
        name="bash_multiline_continuation",
        provenance="scripts/lib/security-trigger.sh:67-68 before this change — "
        "the shape the original line-based sweep could not see",
        language="bash",
        source=(
            '_SECURITY_TRIGGER_REPO="$(_resolve_repo)"\n'
            "changed_files=$(gh pr diff --name-only \"$pr\" \\\n"
            '  --repo "$_SECURITY_TRIGGER_REPO" 2>/dev/null)\n'
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="bash_indirect_taint",
        provenance="a slug laundered through a second variable",
        language="bash",
        source=(
            'BASE="$(_resolve_repo)"\n'
            'MINE="$BASE"\n'
            'gh pr view 1 --repo "$MINE"\n'
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="bash_source_time_leak",
        provenance="scripts/lib/gh-label.sh:37 before this change — an "
        "un-namespaced global that overwrote scripts/sweep-stuck-prs.sh's own",
        language="bash",
        source='REPO="$(_resolve_code_repo)"\n',
        expect_kinds={"source_time_leak"},
        rel_path="scripts/lib/_fixture.sh",
    ),
    Fixture(
        name="bash_gh_variable_kill_switch",
        provenance="scripts/set-ci-kill-switch.sh:102 before this change — the "
        "write half of the CI kill-switch split-brain. `gh variable` was not a "
        "known surface in the detector's first draft, so this fixture is also "
        "the regression test for that gap.",
        language="bash",
        source=(
            '_REPO="$(_resolve_repo)"\n'
            'gh variable set CI_DISABLED --repo "$_REPO" --body "true"\n'
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="bash_graphql_owner_binding",
        provenance="the ${REPO%%/*} → owner:\"$REPO_OWNER\" shape used by "
        "start-the-day.sh, auto-plan.sh and team-lead-iteration.sh. This "
        "detector's first draft reported every such call as UNPINNED and "
        "cleared it; this fixture is the regression test for that blind spot. "
        "A code-plane GraphQL query on a Discussion-plane owner is the defect.",
        language="bash",
        source=(
            'REPO="$(_resolve_repo)"\n'
            'REPO_OWNER="${REPO%%/*}"\n'
            'REPO_NAME="${REPO##*/}"\n'
            "gh api graphql -f query='query { repository(owner:\"'\"$REPO_OWNER\"'\", "
            "name:\"'\"$REPO_NAME\"'\") { pullRequest(number:1) { id } } }'\n"
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="bash_python_in_bash_gh_argv",
        provenance="scripts/drain-pending-prs.sh:92-110 before this change. The "
        "enclosing string is python3 -c \" — DOUBLE-quoted — so bash expands "
        "$_REPO before Python sees it, and this POSTs repos/<plane>/pulls: PR "
        "creation on the wrong repo. Two audits cleared it, the second on my "
        "own wrong claim that the heredoc was single-quoted. Nothing in the "
        "argv looks like a shell `gh ` token, so the token scanner saw zero "
        "call sites in six files carrying this shape.",
        language="bash",
        source=(
            '_REPO="$(_resolve_repo)"\n'
            'HTTP_RESPONSE=$(python3 -c "\n'
            "import subprocess\n"
            "result = subprocess.run(\n"
            "    ['gh', 'api', '-X', 'POST',\n"
            "     'repos/$_REPO/pulls',\n"
            "     '--input', '-'],\n"
            "    capture_output=True)\n"
            '" "$BRANCH")\n'
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="bash_same_line_second_assignment",
        provenance="scripts/lib/pr-dependents.sh:201 before this change. "
        "_ASSIGN_RE needed a ; / && / line-start delimiter, so the second "
        "assignment on a `local` line was invisible: it tainted `pr` with "
        "`repo`'s right-hand side and never recorded `repo` at all. The call "
        "site was silently downgraded from defect to NEEDS CALLER TRACE — a "
        "blind spot that hides defects inside scripts/lib/, which this PR "
        "claimed was clean.",
        language="bash",
        source=(
            "pr_dependents_report() {\n"
            '  local pr="$1" repo="${2:-$(_resolve_repo)}"\n'
            '  echo "retarget: gh pr edit $dep --base main --repo $repo"\n'
            "}\n"
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="typescript_spawnsync_gh",
        provenance="ts-backend/src/loop/loop-phased-step5.ts:109 + :199-203 "
        "before this change — the loop's merge path. The TypeScript surface "
        "was unmeasured by both prior audits because both sweeps were written "
        "for bash and then extended to Python; nobody asked whether a third "
        "language existed.",
        language="typescript",
        source=(
            'import { resolveRepo } from "../config/repo.js";\n'
            "const _REPO = resolveRepo();\n"
            "const r = spawnSync(\n"
            '  "gh",\n'
            '  ["pr", "view", String(pr), "--repo", _REPO,\n'
            '    "--json", "labels"],\n'
            "  { timeout: 20_000 }\n"
            ");\n"
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="typescript_argv_as_single_array",
        provenance="ts-backend/src/spawn/post-merge-hook.ts:609 and 8 more "
        "before this change. The first TS rule required \"gh\" to sit OUTSIDE "
        "the bracket as spawnSync's first argument; this file hands the whole "
        "argv, gh included, to a helper as one array. Nine code-plane sites, "
        "all binding a local `const repo = resolveRepo()`, in a file on "
        "identical footing to loop-phased-step5.ts — which WAS recorded. Same "
        "argv-as-list shape as the Python-in-bash blind spot, in a third "
        "language: the fourth blind spot, and the fourth found by a reviewer "
        "reading code rather than by this detector.",
        language="typescript",
        source=(
            'import { resolveRepo } from "../config/repo.js";\n'
            "const repo = resolveRepo();\n"
            "const out = runGh(\n"
            '  ["gh", "pr", "view", pr, "--repo", repo, "--json", "mergedAt"],\n'
            ");\n"
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="typescript_embedded_python_wrong_plane_fed_in",
        provenance="the false negative a reviewer measured on "
        "ts-backend/src/spawn/post-merge-hook.ts's shape. The embedded Python "
        "`repo` is fed from a DISCUSSION-plane TS variable while an unrelated "
        "`const repo` holds the code plane — a live misroute that reported "
        "`plane=code defect=False` before this change, i.e. reported clean. "
        "The identifier is in Python's scope, so matching it against this "
        "file's declarations was never evidence; it agreed by coincidence of "
        "naming and disagreed the moment the naming changed.",
        language="typescript",
        source=(
            'import { resolveDiscussionRepo, resolveCodeRepo } from "../config/repo.js";\n'
            "const discRepo = resolveDiscussionRepo();\n"
            "const repo = resolveCodeRepo();\n"
            "const py =\n"
            '  "import subprocess, sys\\n" +\n'
            '  "pr = sys.argv[1]\\n" +\n'
            '  "repo = sys.argv[2]\\n" +\n'
            "  \"subprocess.run(['gh', 'pr', 'view', pr, '--repo', repo])\\n\";\n"
            'runShell(["python3", "-c", py, pr, discRepo]);\n'
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="python_argv_list",
        provenance="backend/red_main_check.py:295-297 before this change",
        language="python",
        source=(
            "from backend._repo import REPO\n"
            "import subprocess\n"
            "subprocess.run(\n"
            '    ["gh", "pr", "list", "--state", "open",\n'
            '     "--repo", REPO],\n'
            "    capture_output=True)\n"
        ),
        expect_kinds={"gh_call"},
    ),
    Fixture(
        name="python_api_path_fstring",
        provenance="the `gh api repos/{REPO}/pulls/N` spelling",
        language="python",
        source=(
            "from backend._repo import REPO\n"
            "import subprocess\n"
            'subprocess.run(["gh", "api", f"repos/{REPO}/pulls/1"])\n'
        ),
        expect_kinds={"gh_call"},
    ),
]

# Fixtures that must produce NO defect — a detector that flags everything is
# as useless as one that flags nothing.
NEGATIVE_FIXTURES: list[Fixture] = [
    Fixture(
        name="bash_correct_code_plane",
        provenance="the shape every fixed site should have",
        language="bash",
        source='R="$(_resolve_code_repo)"\ngh pr view 1 --repo "$R"\n',
    ),
    Fixture(
        name="bash_discussion_surface",
        provenance="scripts/start-the-day.sh:591 — a Discussion query bound to "
        "the Discussion plane, correct as-is. The owner: binding matters: an "
        "earlier version of this fixture left the query unpinned, so it was "
        "cleared for being unbound rather than for being on the right plane, "
        "and it could not detect an over-broad surface rule.",
        language="bash",
        source=(
            'R="$(_resolve_repo)"\n'
            'O="${R%%/*}"\n'
            "gh api graphql -f query=\"query { repository(owner:\\\"$O\\\") "
            '{ discussion(number:1) { id } } }"\n'
        ),
    ),
    Fixture(
        name="python_code_plane",
        provenance="a module that imports CODE_REPO",
        language="python",
        source=(
            "from backend._repo import CODE_REPO\n"
            "import subprocess\n"
            'subprocess.run(["gh", "pr", "view", "1", "--repo", CODE_REPO])\n'
        ),
    ),
    Fixture(
        name="typescript_code_plane",
        provenance="the shape a fixed ts-backend call site should have",
        language="typescript",
        source=(
            'import { resolveCodeRepo } from "../config/repo.js";\n'
            "const _REPO = resolveCodeRepo();\n"
            'spawnSync("gh", ["pr", "view", "1", "--repo", _REPO]);\n'
        ),
    ),
]


def run_self_test(verbose: bool = True) -> int:
    """Return 0 when every fixture behaves; non-zero otherwise."""
    failures: list[str] = []

    def _scan(fx: Fixture) -> list[Finding]:
        ext = {"bash": "sh", "python": "py", "typescript": "ts"}[fx.language]
        default = "_fixture." + ext
        fake = REPO_ROOT / (fx.rel_path or default)
        if fx.language == "bash":
            return scan_bash(fake, fx.source)
        if fx.language == "typescript":
            return scan_typescript(fake, fx.source)
        return scan_python(fake, fx.source)

    for fx in FIXTURES:
        found = _scan(fx)
        defects = [f for f in found if f.is_defect]
        kinds = {f.kind for f in defects}
        ok = fx.expect_kinds.issubset(kinds)
        if not ok:
            failures.append(
                f"KNOWN POSITIVE NOT DETECTED: {fx.name} "
                f"(expected kinds {sorted(fx.expect_kinds)}, got {sorted(kinds)}) "
                f"— provenance: {fx.provenance}"
            )
        if verbose:
            print(f"  [{'ok ' if ok else 'FAIL'}] positive: {fx.name} ({fx.language})")

    for fx in NEGATIVE_FIXTURES:
        found = _scan(fx)
        defects = [f for f in found if f.is_defect]
        ok = not defects
        if not ok:
            failures.append(
                f"FALSE POSITIVE: {fx.name} flagged "
                f"{[f.snippet for f in defects]} — provenance: {fx.provenance}"
            )
        if verbose:
            print(f"  [{'ok ' if ok else 'FAIL'}] negative: {fx.name} ({fx.language})")

    # Both languages must actually be exercised.
    langs = {fx.language for fx in FIXTURES}
    for lang in ("bash", "python", "typescript"):
        if lang not in langs:
            failures.append(f"NO KNOWN POSITIVE for language: {lang}")

    if failures:
        print("\nDETECTOR SELF-TEST FAILED — its output cannot be trusted:",
              file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    if verbose:
        print(f"\nself-test: {len(FIXTURES)} known positives "
              f"({len(langs)} languages), {len(NEGATIVE_FIXTURES)} negatives — all pass")
    return 0


# --------------------------------------------------------------------------
# Known-remaining baseline
# --------------------------------------------------------------------------
# The audit found more defects than one reviewable PR should fix at once, so the
# rest are recorded here rather than described in a PR body that nobody re-reads.
# --check fails on any defect in a file that is not in this file, and on any
# file whose defect count has grown. That makes the remaining work impossible to
# lose track of and impossible to add to silently — the failure mode this whole
# Discussion exists to prevent.
#
# Keyed by path and count rather than by line number, so ordinary edits to these
# files do not produce spurious failures; fixing sites only ever lowers a count.
BASELINE_PATH = REPO_ROOT / "scripts" / "repo-plane-known-defects.txt"

# First line of the ledger. Its absence means the file was truncated, emptied
# or replaced — NOT that every defect is fixed. Those two states are
# indistinguishable to anything that merely counts entries, and treating the
# first as the second is what would let the cutover through on a missing file.
LEDGER_MARKER = "REPO-PLANE-LEDGER-V1"


class LedgerError(RuntimeError):
    """The ledger could not be read or is not a ledger. Never an empty list."""


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, int]:
    """Parse the known-defects ledger. Raises rather than returning {}.

    An earlier version returned an empty dict for a missing file, which is the
    skip-on-empty shape: a deleted ledger would have reported "no known
    defects", passed every check, and cleared the cutover.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise LedgerError(
            f"{path} could not be read ({exc}). This is a failure, not an "
            f"empty defect list — an unreadable ledger must never be treated "
            f"as 'everything is fixed'."
        ) from exc

    if LEDGER_MARKER not in text:
        raise LedgerError(
            f"{path} does not carry the {LEDGER_MARKER} marker. The file was "
            f"truncated, emptied or replaced. An entry-free ledger is only "
            f"meaningful when the marker proves the file is intact."
        )

    out: dict[str, int] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.rsplit(None, 1)
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


# --------------------------------------------------------------------------
# The cutover gate
# --------------------------------------------------------------------------
# Setting "code_repo" IS the cutover: it is the single line that turns every
# remaining mis-planed call site into a live misroute, all at the same instant.
# So the constraint "do not set it while defects remain" cannot live in prose.
# A constraint a reader has to find and remember is indistinguishable from one
# that is being honoured, right up until it isn't — which is exactly how the
# post-agent-hook.sh site ended up deferred to a PR that did not cover it.
#
# LIMIT OF THIS GATE — and it is sharper than "there is a corner we miss".
#
# This reads the two in-repo files. backend/_repo_planes.py:_project_json_field
# reads the state-dir project.json FIRST and the repo-root one second, so for
# Python the source this gate CANNOT see OUTRANKS both sources it can. An
# operator who sets "code_repo" in <STATE_DIR>/project.json has cut Python over
# — with no commit, no diff, and this gate still green.
#
# That is not a footnote, it is the gate's actual guarantee: it stops a
# cutover that arrives as a reviewable change, which is how PR-m is planned to
# arrive. It does not stop one applied by hand to runtime state, and it cannot
# — no in-repo check can read an operator's state dir, and tests are forbidden
# from touching the production state tree at all.
#
# Closing that hole needs a runtime check (something the loop or the TUI runs
# at startup, comparing the resolved planes against the ledger), not a
# stronger CI guard. Recorded here so the next person does not mistake this
# gate for complete coverage.
_CUTOVER_FILES = (
    Path(".autonomous-team") / "config.json",
    Path(".autonomous-team") / "project.json",
)


def configured_code_repo(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Every in-repo file that sets a non-empty "code_repo", with its value.

    A missing file means "not configured" and is normal — the open-source
    export ships no .autonomous-team/ at all. A file that exists but cannot be
    parsed is a real error: we must not conclude "not cut over" because the
    config was unreadable.
    """
    found: list[tuple[str, str]] = []
    for rel in _CUTOVER_FILES:
        path = repo_root / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise LedgerError(
                f"{path} exists but could not be parsed ({exc}). Refusing to "
                f"conclude the cutover has not happened from a file this "
                f"check could not read."
            ) from exc
        if isinstance(data, dict):
            value = data.get("code_repo")
            if isinstance(value, str) and value:
                found.append((str(rel), value))
    return found


def cutover_violations(
    repo_root: Path = REPO_ROOT, ledger_path: Path | None = None
) -> list[str]:
    """Empty when the tree is in a legal state; one string per violation.

    Legal states are: code_repo unset (today), or code_repo set with an intact
    ledger carrying zero entries (the cutover is complete and safe).
    """
    ledger = load_baseline(ledger_path or (repo_root / "scripts" /
                                           "repo-plane-known-defects.txt"))
    configured = configured_code_repo(repo_root)
    if not configured:
        return []
    remaining = sum(ledger.values())
    if remaining == 0:
        return []
    files = ", ".join(f"{f} -> {v}" for f, v in configured)
    return [
        f'CUTOVER BLOCKED: "code_repo" is set ({files}) while '
        f"{remaining} known repo-plane defect(s) across {len(ledger)} file(s) "
        f"remain in the ledger. Each of those call sites resolves the "
        f"Discussion plane and spends the slug on a code-plane call, so every "
        f"one of them becomes a live misroute the moment this key takes "
        f"effect. Fix them (see scripts/audit_repo_plane.py) and empty the "
        f"ledger before setting this key."
    ]


def check_against_baseline(defects: list[Finding], baseline: dict[str, int]) -> list[str]:
    """Return human-readable regressions; empty means the tree is no worse."""
    counts: dict[str, int] = {}
    for f in defects:
        counts[f.path] = counts.get(f.path, 0) + 1
    problems: list[str] = []
    for path, n in sorted(counts.items()):
        allowed = baseline.get(path, 0)
        if n > allowed:
            problems.append(
                f"{path}: {n} defect(s), baseline allows {allowed} — "
                f"{'NEW FILE' if allowed == 0 else 'count grew'}"
            )
    for path, allowed in sorted(baseline.items()):
        n = counts.get(path, 0)
        if n < allowed:
            problems.append(
                f"{path}: {n} defect(s) but baseline still allows {allowed} "
                f"— fixed; lower the baseline (this is a good failure)"
            )
    return problems


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _fmt(f: Finding) -> str:
    return (
        f"{f.path}:{f.line}  [{f.surface}/{f.rule}] "
        f"binding={f.binding} plane={f.plane}\n      {f.snippet}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Documented as what it does, not as what it sounds like it does. It used
    # to say "exit 1 if any defect remains" while exiting 0 with 36 recorded —
    # it has always been a *regression* gate, not a zero-defect gate. Fixing
    # the doc rather than the behaviour: a zero-defect gate would be red on
    # every run until the last entry is fixed, and a permanently-red gate gets
    # ignored or disabled, which is how the ratchet would be lost. --strict is
    # there for the caller who genuinely wants "no defects at all".
    ap.add_argument("--check", action="store_true",
                    help="exit 1 on a regression: a defect in a file absent "
                         "from the known-defects ledger, a count above its "
                         "ledger entry, a count left stale below it, or a "
                         "cutover with entries still outstanding. Exits 0 "
                         "while the recorded defects are merely still there.")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if ANY defect remains, ledger or not. This is "
                         "the zero-defect gate --check is not.")
    ap.add_argument("--self-test", action="store_true",
                    help="validate the detector against known-positive fixtures")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--cleared", action="store_true",
                    help="print the cleared set with the evidence that cleared it")
    ap.add_argument("--root", default=str(REPO_ROOT))
    args = ap.parse_args(argv)

    if args.self_test:
        return run_self_test()

    # The self-test always runs before a real scan. An unvalidated clean
    # result is indistinguishable from a broken detector.
    if run_self_test(verbose=False) != 0:
        return 2

    findings = scan_tree(Path(args.root).resolve())
    defects = [f for f in findings if f.is_defect]
    cleared = [f for f in findings if not f.is_defect]

    root = Path(args.root).resolve()

    # Scanning a foreign tree (a base commit, another checkout) is inventory
    # only: that tree has its own ledger or none at all, and neither is this
    # tree's ratchet. Without this the CLI half of --root was still unusable —
    # main() read <root>/scripts/repo-plane-known-defects.txt and exited 2 with
    # LEDGER ERROR, so "point it at a base commit" remained a two-line Python
    # script rather than a flag. The library half worked; the command line did
    # not, which is the half a person reaches for.
    #
    # The ledger checks stay strict for this checkout, where they are the
    # ratchet and where a missing ledger really is a failure.
    foreign = root != REPO_ROOT
    if foreign:
        print(f"note: --root {root} — inventory only. The ledger ratchet and "
              f"cutover gate are checks on THIS checkout and are not applied "
              f"to another tree.", file=sys.stderr)
        regressions = []
    else:
        try:
            regressions = check_against_baseline(defects, load_baseline())
            regressions = cutover_violations(root) + regressions
        except LedgerError as exc:
            print(f"LEDGER ERROR: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(
            {
                "defects": [f.as_dict() for f in defects],
                "cleared": [f.as_dict() for f in cleared],
                "totals": _totals(findings),
                "regressions": regressions,
            },
            indent=2,
        ))
        return 1 if ((args.check and regressions) or (args.strict and defects)) else 0

    totals = _totals(findings)
    print("=" * 74)
    print("repo-plane audit — detector self-test passed before this scan")
    print("=" * 74)
    # Breakdown built from whatever languages are actually present, and
    # asserted to sum to the total — a summary line that does not add up is
    # how a wrong denominator gets quoted downstream.
    _langs = {k: v for k, v in totals.items()
              if k not in ("examined", "defects", "cleared")}
    _breakdown = ", ".join(f"{k} {v}" for k, v in sorted(_langs.items()))
    _sums = sum(_langs.values()) == totals["examined"]
    print(f"call sites examined : {totals['examined']} ({_breakdown})"
          + ("" if _sums else
             f"  [!] breakdown sums to {sum(_langs.values())}, not "
             f"{totals['examined']} — a language is missing from _totals()"))
    print(f"defects             : {totals['defects']}")
    print(f"cleared             : {totals['cleared']}")
    print()

    if defects:
        print("--- DEFECTS " + "-" * 62)
        for f in sorted(defects, key=lambda x: (x.path, x.line)):
            print("  " + _fmt(f))
        print()

    if args.cleared:
        print("--- CLEARED (with the evidence that cleared it) " + "-" * 26)
        for f in sorted(cleared, key=lambda x: (x.path, x.line)):
            reason = _clear_reason(f)
            print(f"  {f.path}:{f.line}  {reason}")
            print(f"      {f.snippet}")
        print()

    if regressions:
        print("--- BASELINE REGRESSIONS " + "-" * 49)
        for r in regressions:
            print("  " + r)
        print()
    elif defects:
        print(f"all {len(defects)} defect(s) are in the known-remaining "
              f"baseline ({BASELINE_PATH.name}) — no regression")
        print()

    return 1 if ((args.check and regressions) or (args.strict and defects)) else 0


def _clear_reason(f: Finding) -> str:
    if f.surface == SURFACE_DISCUSSION:
        return f"cleared: surface is Discussion plane ({f.rule}); binding plane={f.plane}"
    if f.surface == SURFACE_AMBIGUOUS:
        return (f"cleared: surface not code-plane ({f.rule}); binding "
                f"{f.binding} plane={f.plane} — reviewer-checkable")
    if f.plane == CODE:
        return f"cleared: code surface bound to the CODE plane via {f.binding}"
    if f.binding == "LITERAL":
        return "cleared: repo pinned to a literal slug, no resolver involved"
    if f.binding == "UNPINNED":
        return ("cleared: no --repo of its own; inherits the process default "
                "(see the GH_REPO export finding)")
    if f.plane == "unknown":
        # The binding is not assigned anywhere in this file, so its plane is
        # the caller's choice and cannot be settled by reading one file. In
        # this tree these are all function parameters (`local repo="$1"`),
        # variables inherited from a parent script's environment, or GraphQL
        # variable names that only look like shell variables. Each needs a
        # caller trace, which is a human step by design — silently guessing a
        # plane here is how a cleared list becomes wrong.
        return (f"NEEDS CALLER TRACE: {f.binding} is not assigned in this file "
                f"— parameter, inherited env, or non-shell name. Resolve by "
                f"reading the callers, not this line.")
    return f"cleared: binding {f.binding} carries plane={f.plane}"


def _totals(findings: list[Finding]) -> dict:
    """Per-language counts derived from the findings, never hardcoded.

    This had no "typescript" key while the scanner emitted TypeScript findings,
    so the summary printed `examined : 392 (bash 324, python 24)` — a breakdown
    summing to 348 beside a total of 392, with 44 sites in neither column. A
    denominator was then quoted from that line into a review, which is the third
    time in this workstream a call-site total has been carried from the wrong
    place. Building the breakdown from the data means adding a language cannot
    silently drop it again.
    """
    languages = sorted({f.language for f in findings})
    out = {
        "examined": len(findings),
        "defects": sum(1 for f in findings if f.is_defect),
        "cleared": sum(1 for f in findings if not f.is_defect),
    }
    for lang in languages:
        out[lang] = sum(1 for f in findings if f.language == lang)
    return out


if __name__ == "__main__":
    sys.exit(main())
