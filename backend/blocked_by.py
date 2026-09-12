"""backend/blocked_by.py — resolve BLOCKED-BY refs on a Discussion STATUS line.

Parsing the field lives in ``discussion_status.extract_blocked_by``. This module
answers the only question the spawner actually asks: *are any of these refs still
outstanding?*

Ref grammar (verbatim from the STATUS comment):
    #<n>    a pull request, resolved against the CODE plane. Clears when its
            state is MERGED. CLOSED no longer clears (D#2554): on the public
            code plane, CLOSED is the only PR state an outsider can reach on a
            repo they cannot merge into, so treating it as clearing would let
            an open-then-self-close at a predicted number clear a ref. A
            closed-unmerged PR also means the gated work never landed, so
            clearing on it was always the wrong call independent of that.
    D#<n>   a Discussion, resolved against the DISCUSSION plane. Clears when
            its **parsed** status is DONE or CLOSED, so a Discussion quoting
            ``STATUS:DONE`` in prose clears nothing.

Fail closed, without exception. A ref that is malformed, names a number that does
not exist, or cannot be resolved at all (network error, timeout) leaves the
Discussion blocked and carries a human-readable reason. "Unknown" means blocked —
the whole point of D#1755 is that a sequencing constraint which degrades to
"probably fine" is not a constraint.

Batching: every ref in one iteration is resolved in a SINGLE GraphQL query with
two top-level ``repository(...)`` aliases — one pinned to the code plane for PR
refs, one pinned to the Discussion plane for Discussion refs — and results are
memoised per resolver instance. The selector runs each loop iteration, so a
per-ref round trip would put network I/O on a hot path that previously had
none. That holds on the failure path too — see the note in
``partition_spec_ready`` on why the per-Discussion loop must not re-ask.

CLI (called by shell readers):
    python3 backend/blocked_by.py check --stdin   < body
        exit 0  no BLOCKED-BY, or every ref cleared
        exit 2  at least one ref outstanding; reasons printed to stdout
        exit 1  usage error
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.discussion_status import (  # noqa: E402
    BLOCKED_BY_UNPARSEABLE,
    extract_blocked_by,
    extract_status,
)
from backend._repo import (  # noqa: E402
    CODE_REPO as _CODE_REPO,
    REPO_NAME as _REPO_NAME,
    REPO_OWNER as _REPO_OWNER,
)

# A ref is exactly "#123" or "D#123" — nothing else parses.
_REF_RE = re.compile(r"^(D?)#(\d+)$")

# CLOSED intentionally dropped (D#2554) — see the module docstring's ref
# grammar for why. Only MERGED clears a PR ref.
_PR_CLEARED_STATES = {"MERGED"}
_DISCUSSION_CLEARED_STATUSES = {"DONE", "CLOSED"}


def parse_ref(ref: str) -> Optional[tuple[str, int]]:
    """Return ``("pr"|"discussion", number)``, or None when *ref* is malformed."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        return None
    return ("discussion" if m.group(1) else "pr"), int(m.group(2))


def _split_code_repo() -> tuple[str, str]:
    """Split ``_CODE_REPO`` into ``(owner, name)`` at call time, not import
    time, so a config change is picked up without a restart, and so a test can
    monkeypatch the module-level ``_CODE_REPO`` and see it take effect.

    Raises rather than emitting an empty owner/name into the query — that is
    what makes "no ``owner:\"\"``" true by construction (AC10) instead of by
    convention.
    """
    owner, _, name = _CODE_REPO.partition("/")
    if not owner or not name:
        raise RuntimeError(
            f"backend._repo.CODE_REPO ({_CODE_REPO!r}) does not split into a "
            "non-empty owner and name"
        )
    return owner, name


def _default_fetcher(pr_numbers: list[int], discussion_numbers: list[int]) -> dict:
    """One GraphQL round trip, two top-level ``repository(...)`` aliases: PR
    refs resolve against the code plane, Discussion refs against the
    Discussion plane. Only the PR half moves with the cutover — the
    Discussion half stays on ``_REPO_OWNER``/``_REPO_NAME`` on purpose, since
    ``DISCUSSION_REPO`` is legitimately ``""`` on a fork with no private twin
    and ``repository(owner:"")`` is the GraphQL spelling of the ``gh --repo
    ""`` hazard.

    Returns ``{"pr": {n: state}, "discussion": {n: body}, "plane_error": {...}}``.
    A number absent from its mapping did not resolve to a real object and
    therefore blocks. ``plane_error`` names, per kind, the slug of a plane
    whose ``repository(...)`` alias itself came back null — distinct from a
    single number not resolving inside a plane that *did* resolve (AC11).
    """
    pr_fields = [f'p{n}: pullRequest(number:{n}) {{ state }}' for n in pr_numbers]
    disc_fields = [f'd{n}: discussion(number:{n}) {{ body }}' for n in discussion_numbers]
    if not pr_fields and not disc_fields:
        return {"pr": {}, "discussion": {}, "plane_error": {}}

    blocks = []
    if pr_fields:
        code_owner, code_name = _split_code_repo()
        blocks.append(
            'code: repository(owner:"%s", name:"%s") { %s }'
            % (code_owner, code_name, " ".join(pr_fields))
        )
    if disc_fields:
        blocks.append(
            'disc: repository(owner:"%s", name:"%s") { %s }'
            % (_REPO_OWNER, _REPO_NAME, " ".join(disc_fields))
        )

    query = "query { %s }" % " ".join(blocks)
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Partial GraphQL responses are normal here: asking for a nonexistent PR,
    # or for a repo alias that can't resolve at all, both return `errors`
    # alongside a `data` block whose other aliases resolved fine. Parse what
    # came back and let absent aliases/numbers fail closed below.
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"gh api graphql returned unparseable output: {proc.stderr.strip()[:200]}")
    data = payload.get("data") or {}

    code_node = data.get("code") if pr_fields else None
    disc_node = data.get("disc") if disc_fields else None

    # Total failure carryover: if every plane we actually queried came back
    # missing/null AND the process itself signalled failure, this is not a
    # per-plane resolution problem, it's the fetch itself — raise so the whole
    # batch fails closed via BlockerResolver's (uncached, no-retry) fetch_error
    # path. A single failed plane while the other resolved is handled below
    # instead, without raising, so the surviving plane still answers normally.
    queried = [n for n, fields in (("pr", pr_fields), ("discussion", disc_fields)) if fields]
    node_for = {"pr": code_node, "discussion": disc_node}
    if queried and all(node_for[k] is None for k in queried) and proc.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {proc.stderr.strip()[:200]}")

    prs, discs, plane_error = {}, {}, {}
    if pr_fields:
        if code_node is None:
            plane_error["pr"] = _CODE_REPO
        else:
            for n in pr_numbers:
                node = code_node.get(f"p{n}")
                if node:
                    prs[n] = node.get("state", "")
    if disc_fields:
        if disc_node is None:
            plane_error["discussion"] = f"{_REPO_OWNER}/{_REPO_NAME}"
        else:
            for n in discussion_numbers:
                node = disc_node.get(f"d{n}")
                if node:
                    discs[n] = node.get("body", "")
    return {"pr": prs, "discussion": discs, "plane_error": plane_error}


def _emit_cleared_audit(kind: str, number: int, state: str, slug: str, resolver_source: str) -> None:
    """Best-effort audit row for a ref that just cleared (AC8/AC9).

    Imported lazily, inside the function, not at module scope: this module
    must stay importable with no state dir at all — ``backend.state_paths``
    raises under pytest when ``AUTONOMOUS_TEAM_STATE_DIR`` is unset, and
    resolving a repo slug must work everywhere blocked_by.py is imported.
    Wrapped in a bare ``except`` end to end: an audit write must never block,
    delay, or crash ref resolution, so a missing/unwritable state dir is
    silently inert here rather than surfacing.
    """
    try:
        from backend.audit_trail import get_audit_trail  # noqa: PLC0415

        get_audit_trail().emit(
            source="gate",
            action="blocked_by_cleared",
            key=f"{kind}#{number}",
            old_value=None,
            new_value={
                "repo": slug,
                "kind": kind,
                "number": number,
                "state": state,
                "resolver_source": resolver_source,
            },
            actor="blocked_by",
            # Dedupes through _check_seen_audit's 7-day TTL: a permanently
            # cleared ref records roughly once a week, not once per loop
            # iteration, and a state change still emits a fresh row.
            event_id=f"blocked_by:{slug}:{kind}:{number}:{state}",
        )
    except Exception:  # noqa: BLE001 — audit is best-effort, never load-bearing
        pass


class BlockerResolver:
    """Resolves BLOCKED-BY refs with one batched round trip and a memo cache.

    *fetcher* is injectable so tests can assert the call count without touching
    the network.
    """

    def __init__(self, fetcher: Optional[Callable[[list[int], list[int]], dict]] = None):
        self._fetcher = fetcher or _default_fetcher
        # Keyed by the bare ref string ("#123" / "D#123"), not by (kind, plane,
        # number). That stays complete rather than becoming plane-unaware: the
        # ref string already encodes the kind (the "D" prefix), and which
        # plane answers a given kind is a pure function of kind (PR -> code
        # plane, Discussion -> Discussion plane) — never a per-ref choice. Do
        # not redesign this into a richer key.
        self._cache: dict[str, Optional[str]] = {}
        self.call_count = 0

    def unresolved(self, refs: Iterable[str]) -> list[tuple[str, str]]:
        """Return ``[(ref, reason)]`` for every ref still blocking.

        An empty list means the Discussion is clear to proceed.
        """
        refs = list(refs)
        if not refs:
            return []

        parsed = {ref: parse_ref(ref) for ref in refs}
        need_pr, need_disc = [], []
        for ref, p in parsed.items():
            if p is None or ref in self._cache:
                continue
            (need_pr if p[0] == "pr" else need_disc).append(p[1])

        data: dict = {"pr": {}, "discussion": {}}
        fetch_error: Optional[str] = None
        if need_pr or need_disc:
            self.call_count += 1
            try:
                data = self._fetcher(sorted(set(need_pr)), sorted(set(need_disc)))
            except Exception as exc:  # noqa: BLE001 — every failure mode blocks
                fetch_error = f"{type(exc).__name__}: {exc}"

        out: list[tuple[str, str]] = []
        for ref in refs:
            reason = self._verdict(ref, parsed[ref], data, fetch_error)
            if reason is not None:
                out.append((ref, reason))
        return out

    def _verdict(self, ref, parsed, data, fetch_error) -> Optional[str]:
        """None when the ref has cleared; otherwise the reason it still blocks."""
        if ref == BLOCKED_BY_UNPARSEABLE:
            # extract_blocked_by saw the field but got no usable ref out of it.
            # Say that plainly rather than calling it a malformed ref — there is
            # no ref here, and the operator's fix is to the STATUS line itself.
            return "BLOCKED-BY is present but its value is empty or unreadable — fix the STATUS line"
        if parsed is None:
            return "malformed ref (expected #<pr> or D#<discussion>)"
        if ref in self._cache:
            return self._cache[ref]

        kind, number = parsed
        if fetch_error is not None:
            # Deliberately NOT cached: a transient failure must not pin this ref
            # to blocked for the rest of the process.
            return f"could not resolve — {fetch_error}"

        plane_error = data.get("plane_error") or {}
        if kind == "pr":
            state = data.get("pr", {}).get(number)
            plane_err = plane_error.get("pr")
            if state is None:
                if plane_err:
                    # Distinct from "not found" (AC11): the code plane's own
                    # repository(...) alias came back null, so nothing in this
                    # batch could be checked — not just this one number.
                    reason = f"PR #{number} could not be checked — code plane {plane_err} failed to resolve"
                else:
                    reason = f"PR #{number} not found"
            elif state.upper() in _PR_CLEARED_STATES:
                reason = None
                _emit_cleared_audit("pr", number, state, _CODE_REPO, "backend._repo.CODE_REPO")
            elif state.upper() == "CLOSED":
                # CLOSED no longer clears (D#2554) — say so explicitly rather
                # than folding it into the generic "is {state}" message, so an
                # operator reads "edit the line" instead of "wait it out".
                reason = f"PR #{number} is CLOSED (not merged) — edit the STATUS line, it will not clear on its own"
            else:
                reason = f"PR #{number} is {state}"
        else:
            body = data.get("discussion", {}).get(number)
            plane_err = plane_error.get("discussion")
            if body is None:
                if plane_err:
                    reason = f"Discussion #{number} could not be checked — Discussion plane {plane_err} failed to resolve"
                else:
                    reason = f"Discussion #{number} not found"
            else:
                # extract_status, not a substring test: a Discussion that merely
                # quotes STATUS:DONE in prose must not clear anything (item 5).
                status = extract_status(body)
                if status in _DISCUSSION_CLEARED_STATUSES:
                    reason = None
                    _emit_cleared_audit(
                        "discussion", number, status, f"{_REPO_OWNER}/{_REPO_NAME}", "REPO_OWNER+REPO_NAME"
                    )
                else:
                    reason = f"D#{number} is {status}"

        self._cache[ref] = reason
        return reason


def unresolved_for_body(body: str, resolver: Optional[BlockerResolver] = None) -> list[tuple[str, str]]:
    """Convenience: parse *body*'s BLOCKED-BY field and resolve it in one step."""
    return (resolver or BlockerResolver()).unresolved(extract_blocked_by(body))


def format_reasons(blocked: list[tuple[str, str]]) -> str:
    """Render ``[(ref, reason)]`` as a single-line, human-readable summary."""
    return "; ".join(f"{ref} ({reason})" for ref, reason in blocked)


def partition_spec_ready(discussions: Iterable[dict], resolver: Optional[BlockerResolver] = None):
    """Split raw Discussion nodes into (spawnable, blocked).

    *discussions* are dicts with ``number``/``title``/``body``. Returns
    ``(ready, blocked)`` where ready is ``[{"number","title"}]`` and blocked is
    ``[(number, reasons)]``.

    This is the ONE implementation of "may the loop pick this up". Both the
    snapshot path and the GraphQL fallback in scripts/loop-phased-step5.sh call
    it, so the two cannot drift apart — which is the failure D#1755 was filed
    about. One shared resolver across the whole batch keeps this to a single
    round trip per iteration.
    """
    from backend.discussion_status import is_spec_ready  # noqa: PLC0415

    resolver = resolver or BlockerResolver()
    candidates = [
        (d, extract_blocked_by(d.get("body", "")))
        for d in discussions
        if is_spec_ready(d.get("body", ""))
    ]

    # Resolve every ref in the batch with one call, then answer each Discussion
    # from that single result. The loop below deliberately does NOT re-enter the
    # resolver: `_verdict` does not cache a fetch failure (a transient error must
    # not pin a ref to blocked for the life of the process), so re-asking per
    # Discussion meant one failing GitHub call was retried once per blocked
    # Discussion — K+1 fetches, each carrying `_default_fetcher`'s 60s timeout,
    # on a path the loop selector runs every iteration. Scoping the failure to
    # the batch keeps that at one call, and leaves the resolver's own
    # don't-cache-failures behaviour (and its test) untouched.
    all_refs = [ref for _, refs in candidates for ref in refs]
    reason_by_ref = dict(resolver.unresolved(all_refs)) if all_refs else {}

    ready, blocked = [], []
    for d, refs in candidates:
        outstanding = [(ref, reason_by_ref[ref]) for ref in refs if ref in reason_by_ref]
        if outstanding:
            blocked.append((d.get("number", 0), format_reasons(outstanding)))
        else:
            ready.append({"number": d.get("number", 0), "title": d.get("title", "")})
    return ready, blocked


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "check" or sys.argv[2] != "--stdin":
        print(f"Usage: {sys.argv[0]} check --stdin", file=sys.stderr)
        sys.exit(1)
    _blocked = unresolved_for_body(sys.stdin.read())
    if _blocked:
        print(format_reasons(_blocked))
        sys.exit(2)
    sys.exit(0)
