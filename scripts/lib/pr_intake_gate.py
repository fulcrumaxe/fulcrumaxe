#!/usr/bin/env python3
"""scripts/lib/pr_intake_gate.py — HG-7 for pull requests: decide, from the
GitHub-authenticated PR author, whether automation may act on a PR at all.

WHY THIS EXISTS (D#2404)
------------------------
`external_intake_gate.py` gates **Discussions** by author, and a PR inherits
that gate only through its *originating Discussion*
(`loop-phased-step5.sh::_external_provenance_forces_security`). A PR opened
directly on the code repo has no originating Discussion, so nothing in the
pickup path ever looked at who wrote it:

    scripts/team-lead-iteration.sh  Step 4   `gh pr list` — no author filter
    scripts/team-lead-iteration.sh  Step 5.3 quality gate WRITES labels
    scripts/sweep-stuck-prs.sh               enqueues an executor respawn

After the D#2348 cutover those three act on strangers' PRs exactly as they act
on ours. This module is the author check that chain was missing.

WHAT IT DOES — one mechanism, second surface
--------------------------------------------
The decision is the same one `should_block_spawn()` already makes for
Discussions, fed by the same trust set:

    resolve_allowlist()  =  collaborators(push|admin) ∪ {bot, boss}
                            ∪ config.maintainer_allowlist

  * author in the trust set        -> provenance internal, not blocked
  * author outside it, no label     -> BLOCKED (external_awaiting_intake_approval)
  * author outside it, label applied
    by a trusted account            -> allowed, and security_required=True
  * author outside it, label applied
    by anyone else                  -> BLOCKED (D#2404 AC3)

No second trust model is introduced: `is_trusted_author()` comes from
`pr_comment_trust.py` (#2375) and `should_block_spawn()`/`resolve_allowlist()`
from `external_intake_gate.py`. This module is wiring, not policy.

LIVE IDENTITY, NEVER A LABEL (D#1588 Risk 3, D#2404 AC3)
--------------------------------------------------------
Provenance is re-derived from the PR's live author on every call. A
`provenance:internal` label on the PR is *never* read as trust — it is not
consulted at all. A stale or attacker-applied label therefore cannot fail
open, which is the scan-lag hole `check_discussion()` already closes on the
Discussion side.

The one label that IS consulted is `intake-approved`, and only as a human's
"yes". Because a label is a claim about who approved, not proof, this module
also reads the issue-events timeline and requires the account that *applied*
`intake-approved` to be in the trust set. On GitHub an author without triage
permission cannot label their own PR — this check is what makes that a
property of the gate rather than a property of GitHub's current permission
model.

WHICH REPO'S COLLABORATORS DECIDE TRUST
---------------------------------------
The PR lives on the CODE plane; the trust set is resolved from the
**Discussion** plane, which is `resolve_allowlist()`'s default. That is
deliberate and is `external_intake_gate._resolve_default_discussion_repo_slug`'s
own argument applied here: after the cutover the code repo is public, and push
on a public repo is routinely granted to outside contributors who have no
standing to drive our automation. "May drive automation" must mean the same
set of people on both surfaces, so it is keyed to the plane we administer.

Note for reviewers: `pr_comment_trust.py` resolves the same set against
CODE_REPO instead. The two are the same repo today and diverge only after the
cutover. Flagged, not silently changed — it is #2375's call site, not this
one's.

FAIL CLOSED
-----------
Anything unreadable — the PR, its labels, the timeline, the trust set —
blocks. `blocked: true` with a reason naming what could not be read. A gate
that cannot see is a gate that says no.

CLI
---
    python3 scripts/lib/pr_intake_gate.py check-pr <N> [--repo SLUG]
        prints JSON; exit 0 = may be worked, 1 = blocked

    python3 scripts/lib/pr_intake_gate.py security-required-pr <N> [--repo SLUG]
        prints true/false/unknown; exit 0 = required, 1 = confirmed not
        required, 3 = unknown (fail closed — callers treat as required),
        matching external_intake_gate.py's `security-required` contract.

    python3 scripts/lib/pr_intake_gate.py rebaseline-pr <N> [--repo SLUG]
        re-approves the PR at its CURRENT head (D#2421) — the recovery
        command for a drifted or ceiling-blocked PR. A deliberate local
        operator action; never triggered by a re-label. Does not clear the
        invalidation ceiling — see pr_head_baseline.rebaseline()'s docstring.

HEAD-SHA BASELINE (D#2421)
---------------------------
`intake-approved` used to be bound to the PR number alone: a maintainer
approves at head A, the author force-pushes to head B, and automation still
spawns on B — content nobody reviewed. `scripts/lib/pr_head_baseline.py` (a
new module, `external_intake_gate.py` and `intake_baseline.py` both
unmodified) binds the approval to the commit SHA `fetch_pr_meta` already
reads, and the result is fed into `should_block_spawn()`'s existing
`baseline_verdict` keyword — the same seam the Discussion side already uses.
See that module's docstring for why SHA (not tree, not a timestamp) and why a
sibling store file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from external_intake_gate import (  # noqa: E402
    INTAKE_APPROVED_LABEL,
    PROVENANCE_EXTERNAL,
    PROVENANCE_INTERNAL,
    resolve_allowlist,
    should_block_spawn,
)
from pr_comment_trust import is_trusted_author  # noqa: E402
import pr_head_baseline  # noqa: E402

#: Reason strings. Kept as constants because both the bash callers and the
#: tests match on them, and a typo in either would silently read as "some
#: other reason" rather than as a failure.
REASON_INTERNAL = "internal"
REASON_AWAITING = "external_awaiting_intake_approval"
REASON_APPROVED = "external_approved"
REASON_UNTRUSTED_APPROVER = "external_intake_approval_untrusted_actor"
REASON_PR_UNREADABLE = "pr_meta_unreadable"
REASON_TIMELINE_UNREADABLE = "intake_approval_actor_unreadable"
#: D#2421 — the head moved after a trusted account approved it.
REASON_HEAD_CHANGED = "external_pr_head_changed_after_approval"
#: D#2421 — the head-baseline store could not be read or written; fail closed.
#: Covers both "no stored row and recording one failed" and "the store itself
#: is unreadable" — neither reason string may contain a filesystem path
#: (Spec constraint), and this one is a fixed constant so it never does.
REASON_HEAD_UNRECORDED = "external_pr_head_unrecorded"
#: D#2421 — the PR has drifted onto too many distinct new heads since its
#: last approved baseline; blocked until a human runs rebaseline-pr.
REASON_CEILING = "external_pr_head_invalidation_ceiling"


def _default_code_repo() -> str:
    """PRs live on the CODE plane — resolve it, never hard-code it."""
    sys.path.insert(0, str(_REPO_ROOT))
    from backend._repo import CODE_REPO  # noqa: PLC0415

    return CODE_REPO


def _gh(args: list) -> str:
    """Run `gh` and return stdout. Raises on any non-zero exit, so a failed
    read can never be mistaken for an empty result."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def fetch_pr_meta(pr: int, repo_slug: str, *, gh=None) -> dict:
    """Live author + labels + head SHA for *pr*, in one API call.

    Returns ``fetch_ok: False`` rather than raising, so callers get a blocked
    verdict with a reason instead of a traceback. The author is read from the
    REST `user.login` field — GitHub's authenticated author — never from any
    body, title or branch text.

    ``head_sha`` (D#2421) is `raw["head"]["sha"]` — the same `pulls/{pr}`
    response already fetched here, so this costs zero net-new API calls. It
    is `None` when absent or not a string, never coerced to any other value;
    callers must treat that as "no fingerprint available", not as a match.
    """
    call = gh or _gh
    try:
        raw = json.loads(call(["api", f"repos/{repo_slug}/pulls/{pr}"]) or "{}")
    except Exception as exc:  # noqa: BLE001 — fail closed
        return {
            "pr": pr,
            "author": None,
            "labels": [],
            "head_sha": None,
            "fetch_ok": False,
            "error": str(exc)[:200],
        }

    holder = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    login = holder.get("login")
    labels = [
        entry.get("name")
        for entry in (raw.get("labels") or [])
        if isinstance(entry, dict) and entry.get("name")
    ]
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = None
    return {
        "pr": pr,
        "author": login.strip() if isinstance(login, str) and login.strip() else None,
        "author_id": holder.get("id"),
        "labels": labels,
        "head_sha": head_sha,
        "fetch_ok": True,
    }


def intake_approval_actor(pr: int, repo_slug: str, *, gh=None) -> tuple[Optional[str], bool]:
    """Who applied `intake-approved` to *pr*, per the issue-events timeline.

    Returns ``(login, read_ok)``. The most recent `labeled` event for that
    label wins — a re-application by a maintainer after an author's own
    attempt should count, and the latest event is the one that reflects the
    current label. ``(None, True)`` means the timeline was read and contains
    no such event (the label was applied by a path that leaves no event, or
    was never applied); that is not an approval either.
    """
    call = gh or _gh
    try:
        events = json.loads(call(["api", "--paginate", f"repos/{repo_slug}/issues/{pr}/events"]) or "[]")
    except Exception:  # noqa: BLE001 — fail closed
        return None, False
    if not isinstance(events, list):
        return None, False

    actor: Optional[str] = None
    stamp = ""
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "labeled":
            continue
        label = event.get("label") or {}
        if not isinstance(label, dict) or label.get("name") != INTAKE_APPROVED_LABEL:
            continue
        created = event.get("created_at") or ""
        if created >= stamp:
            stamp = created
            holder = event.get("actor") if isinstance(event.get("actor"), dict) else {}
            login = holder.get("login")
            actor = login.strip() if isinstance(login, str) and login.strip() else None
    return actor, True


def _pr_baseline_key(repo_slug: str, pr: int) -> str:
    """Baseline-store key for *pr* — delegates to pr_head_baseline so the key
    shape stays owned by the module that also owns the store path (D#2421)."""
    return pr_head_baseline.pr_key(repo_slug, pr)


def check_pr(
    pr: int,
    repo_slug: Optional[str] = None,
    *,
    gh=None,
    allowlist: Optional[set] = None,
    baseline_path=None,
) -> dict:
    """The gate. Returns the same shape as
    ``external_intake_gate.check_discussion()`` plus ``security_required``.

    ``blocked: True`` means inert to automation: no agent spawned, no label
    applied, not listed as work.

    ``baseline_path`` (D#2421) overrides the head-SHA baseline store path —
    tests use it for isolation, exactly like ``intake_baseline``'s own
    ``path=`` parameter; production callers leave it unset and
    ``pr_head_baseline`` resolves the real store.
    """
    slug = repo_slug or _default_code_repo()
    try:
        trust = allowlist if allowlist is not None else resolve_allowlist()
    except Exception as exc:  # noqa: BLE001 — fail closed
        return {
            "pr": pr,
            "repo": slug,
            "author": None,
            "provenance": PROVENANCE_EXTERNAL,
            "blocked": True,
            "reason": "trust_set_unresolvable",
            "security_required": True,
            "error": str(exc)[:200],
        }

    meta = fetch_pr_meta(pr, slug, gh=gh)
    if not meta["fetch_ok"]:
        return {
            "pr": pr,
            "repo": slug,
            "author": None,
            "provenance": PROVENANCE_EXTERNAL,
            "blocked": True,
            "reason": REASON_PR_UNREADABLE,
            "security_required": True,
            "error": meta.get("error", ""),
        }

    author = meta["author"]
    provenance = PROVENANCE_INTERNAL if is_trusted_author(author, trust) else PROVENANCE_EXTERNAL
    trust_casefold = {entry.casefold() for entry in trust if isinstance(entry, str) and entry}
    label_names = meta["labels"]

    # D#2421: for an external, labeled PR, the label is only a real approval
    # if BOTH (a) a trusted account applied it (D#2404 AC3) AND (b) the head
    # it approved is still the head we are looking at. (a) needs the
    # issue-events timeline read that already existed; (b) is new and reads
    # only the local baseline store — no additional GitHub API call.
    baseline_verdict: Optional[str] = None
    approver_blocked_reason: Optional[str] = None

    if provenance == PROVENANCE_EXTERNAL and INTAKE_APPROVED_LABEL in set(label_names):
        actor, read_ok = intake_approval_actor(pr, slug, gh=gh)
        if not read_ok:
            approver_blocked_reason = REASON_TIMELINE_UNREADABLE
        elif not is_trusted_author(actor, trust):
            approver_blocked_reason = REASON_UNTRUSTED_APPROVER
        else:
            key = _pr_baseline_key(slug, pr)
            head_sha = meta.get("head_sha")
            if isinstance(head_sha, str) and head_sha:
                baseline_verdict = pr_head_baseline.check_and_record(key, head_sha, path=baseline_path)
            else:
                # No fingerprint to compare against — cannot confirm the
                # approved head is still current. Fail closed (HG-1), never
                # silently treated as a match.
                baseline_verdict = "unknown"

    if baseline_verdict == "ceiling":
        blocked, reason = True, REASON_CEILING
    elif approver_blocked_reason is not None:
        blocked, reason = True, approver_blocked_reason
    else:
        # Same decision function the Discussion side uses. It reads the real
        # label list and nothing else — no body text, no title, no branch
        # name. It is fed a casefolded author so its exact-membership test
        # agrees with is_trusted_author's casefolded one; GitHub logins are
        # unique case-insensitively, so casefolding closes a spelling bypass
        # without widening the set.
        blocked, reason = should_block_spawn(
            (author or "").casefold() or None,
            label_names,
            trust_casefold,
            baseline_verdict=baseline_verdict,
        )
        if baseline_verdict is not None:
            # Only reached for an external, labeled PR whose label was
            # applied by a trusted account — remap should_block_spawn's
            # generic reasons to the PR-specific ones the Spec requires.
            if not blocked:
                reason = REASON_APPROVED
            elif baseline_verdict == "drifted":
                reason = REASON_HEAD_CHANGED
            elif baseline_verdict == "unknown":
                reason = REASON_HEAD_UNRECORDED

    return {
        "pr": pr,
        "repo": slug,
        "author": author,
        "provenance": provenance,
        "blocked": blocked,
        "reason": reason,
        # HG-7 parity: an externally-authored PR makes security-review-passed a
        # hard merge requirement even once a human has let it through. D#2421
        # AC-9: unchanged by any outcome above — merge-side protection stays
        # keyed on provenance alone.
        "security_required": provenance == PROVENANCE_EXTERNAL,
    }


def rebaseline_pr(
    pr: int, repo_slug: Optional[str] = None, *, gh=None, baseline_path=None
) -> dict:
    """The recovery command (D#2421): re-approve *pr* at its CURRENT head.

    A deliberate local operator action — never triggered by a re-label (see
    pr_head_baseline.rebaseline()'s docstring for why). Does not clear the
    invalidation ceiling; a PR that has hit it stays blocked with
    ``REASON_CEILING`` after this call.
    """
    slug = repo_slug or _default_code_repo()
    meta = fetch_pr_meta(pr, slug, gh=gh)
    if not meta["fetch_ok"]:
        return {"pr": pr, "repo": slug, "ok": False, "reason": REASON_PR_UNREADABLE}

    head_sha = meta.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return {"pr": pr, "repo": slug, "ok": False, "reason": "pr_head_sha_unavailable"}

    key = _pr_baseline_key(slug, pr)
    try:
        pr_head_baseline.rebaseline(key, head_sha, path=baseline_path)
    except Exception as exc:  # noqa: BLE001 — report, never raise past the CLI boundary
        return {"pr": pr, "repo": slug, "ok": False, "reason": "rebaseline_failed", "error": str(exc)[:200]}

    return {"pr": pr, "repo": slug, "ok": True, "head_sha": head_sha}


def _main(argv: list) -> int:
    valid_cmds = ("check-pr", "security-required-pr", "rebaseline-pr")
    if len(argv) < 2 or argv[1] not in valid_cmds:
        sys.stderr.write(
            "Usage:\n"
            "  python3 scripts/lib/pr_intake_gate.py check-pr <N> [--repo SLUG]\n"
            "  python3 scripts/lib/pr_intake_gate.py security-required-pr <N> [--repo SLUG]\n"
            "  python3 scripts/lib/pr_intake_gate.py rebaseline-pr <N> [--repo SLUG]\n"
        )
        return 2

    cmd = argv[1]
    rest = argv[2:]
    slug = None
    if "--repo" in rest:
        idx = rest.index("--repo")
        try:
            slug = rest[idx + 1]
        except IndexError:
            sys.stderr.write("--repo requires a value\n")
            return 2
        rest = rest[:idx] + rest[idx + 2 :]
    if not rest:
        sys.stderr.write(f"{cmd} requires a PR number\n")
        return 2
    try:
        pr = int(rest[0])
    except ValueError:
        sys.stderr.write(f"{cmd}: '{rest[0]}' is not a PR number\n")
        return 2

    if cmd == "rebaseline-pr":
        result = rebaseline_pr(pr, slug)
        print(json.dumps(result))
        return 0 if result["ok"] else 1

    result = check_pr(pr, slug)

    if cmd == "check-pr":
        print(json.dumps(result))
        return 1 if result["blocked"] else 0

    # security-required-pr: exit-code contract mirrors external_intake_gate.py's
    # `security-required` so merge-gate callers need no new branch shape.
    if result["reason"] in (REASON_PR_UNREADABLE, "trust_set_unresolvable"):
        print("unknown")
        return 3
    if result["security_required"]:
        print("true")
        return 0
    print("false")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv))
