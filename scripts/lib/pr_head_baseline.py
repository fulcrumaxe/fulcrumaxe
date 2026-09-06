"""scripts/lib/pr_head_baseline.py — bind `intake-approved` on a pull request
to a commit SHA, not just to the PR number (D#2421).

The binding is a bounded race, not a verified head: see the section below
titled "THIS IS A BOUNDED RACE, NOT A VERIFIED HEAD" before treating any
verdict this module returns as proof that a human reviewed the recorded
commit.

WHY THIS EXISTS
---------------
`pr_intake_gate.check_pr` used to call `should_block_spawn(..., baseline_verdict=None)`
— always the default — so a labeled, trusted-approved external PR stayed
"approved" no matter how many times its head moved afterward. A maintainer
reviews at head A, approves; the author force-pushes to head B; automation
still spawns on B, which nobody reviewed.

`should_block_spawn()` (`external_intake_gate.py`) already has the seam this
needed: a keyword-only `baseline_verdict` parameter. This module is the PR-side
producer of that verdict — it never touches `external_intake_gate.py` or
`intake_baseline.py`, both of which stay exactly as they are (every
`intake_baseline` function already takes a `path=` override, which is what
makes a *sibling* store possible with zero edits to that module).

WHY A SIBLING STORE, NOT THE SAME FILE
---------------------------------------
`intake_baseline._KEY_RE` is `^[^/\\s#]+/[^/\\s#]+#\\d+$` — a PR key
("owner/name#7") and a Discussion key ("owner/name#7") are the identical
shape, and today they differ only by which slug each plane resolves to. But
CLAUDE.md documents a supported revert that points `code_repo` back at the
private slug, at which point a PR #7 and a Discussion #7 key would collide in
one shared store. Routing PR baselines through their own file (this module's
`_default_store_path()`) makes that collision structurally impossible without
touching `intake_baseline.py` at all — the row identity is never shared,
regardless of what the two keys look like.

SHA, NEVER A TIMESTAMP OR A TREE
---------------------------------
The fingerprint is the commit SHA `fetch_pr_meta` already reads off the
`pulls/{pr}` REST response (`head.sha`) — zero net-new GitHub API calls. It is
passed through `intake_baseline.check_baseline()` verbatim as `content_sha256`,
with `last_edited_at=None` and `edit_count=0` so the other two invalidation
signals in that function are always false by construction — the verdict
reduces to pure SHA-string equality. That is deliberate: `committer.date` on a
timeline `committed` event is attacker-controlled (a force-pusher can set it
to 1970 and make a hostile push look like it predates approval), and tree
equality lets an attacker pick the comparison input (an amended commit can
keep an identical tree while still being new, attacker-authored content in
every other sense — message, parent, signature). Comparing the SHA string
itself has neither hole.

THE CEILING
-----------
`intake_baseline.bump_invalidation()` has no caller anywhere that compares its
return value to a threshold — copying that pattern verbatim to the PR side
would hand an external author an unbounded force-push / re-approve loop, since
each new head is a fresh "awaiting approval" cycle for a human to clear. This
module adds the missing brake: once a PR has drifted onto `CEILING` (3)
distinct new heads, every further observation returns `"ceiling"` — blocked,
regardless of what a maintainer does next — until `rebaseline()` is invoked
AND the invalidation counter is reset by a fresh call to `record_baseline()`
that ships with a lower carried-forward count... except it never is: per
`intake_baseline.record_baseline()`, `invalidation_count` is always carried
forward from the existing row. `rebaseline()` intentionally does not reset it.
That is what makes the ceiling a *ceiling* rather than a counter that a
re-approval quietly clears — the same distinction cost-analyst raised against
the Discussion-side counter that has no consumer at all.

The counter advances once per *distinct* new head, not once per poll pass:
`dismissed_content_sha256` (already on the stored row, already used by
`external_intake_gate._reconcile_baseline` for exactly this purpose) is reused
here as the same de-duplication marker, so ten observations of one unchanged
drifted head bump the counter once, not ten times.

Never returns "absent". A labeled PR with no stored row is the precise
condition that reopened the original bug (`should_block_spawn`'s `absent` arm
maps to "not blocked"), so a first observation never reaches that arm: it
either auto-baselines to the current head and reports `"match"`, or refuses
to auto-baseline and reports `"unknown"` (see the next section for which).
The caller (`pr_intake_gate.py`) therefore only ever receives one of
`{"match", "drifted", "unknown", "ceiling"}`.

THIS IS A BOUNDED RACE, NOT A VERIFIED HEAD (bounded by D#2421 PR 3)
--------------------------------------------------------------------
A first observation is auto-baselined only when the `intake-approved` label
landed within `FIRST_OBSERVATION_GRACE_SECONDS` of our own clock — GitHub
stamps the `labeled` event's `created_at` server-side, so that value is not
forgeable by the PR author. A first observation of a label older (or, by the
same symmetric comparison, further in the future) than that grace refuses to
auto-baseline: it writes no row and returns `"unknown"`, which the caller maps
to `external_pr_head_unrecorded` and a blocked verdict.

**The residual window is `FIRST_OBSERVATION_GRACE_SECONDS`; it is not zero.**
An attacker who watches for the label and force-pushes *inside* that grace
still has the hostile head recorded as the baseline. What D#2421 PR 3 removed
is the unbounded case: before it, the window was "however long the stored row
has been missing", which after a state-dir wipe is forever and admits an
arbitrarily later head. The head this module records is whatever was live when
the gate first looked, within that margin — nothing here establishes that any
human read it.

No commit-supplied timestamp participates in that comparison, deliberately.
`committer.date` and `author.date` are attacker-controlled: a force-pusher
sets either to a value that predates the label, and a gate that compared
against them would report a *confident* pass where an honest one reports
"unrecorded". Only the server-stamped label time and `datetime.now(timezone.utc)`
are read.

The operational cost is real and falls on the operator. Any loss of the
runtime state dir empties this store, so every already-approved external PR
has no row and a label far older than the grace — all of them fail closed at
once, and each needs an operator to run `pr_intake_gate.py rebaseline-pr <N>`
after checking that the PR's *current* head is the one they read. There is no
bulk recovery, on purpose: re-approving every open external PR at whatever
head it currently holds is the bypass this whole mechanism exists to prevent.

A malformed or missing `FIRST_OBSERVATION_GRACE_SECONDS` resolves to `0`
(see `_first_observation_grace`), which refuses every first observation. A
broken configuration blocks; it never widens the window.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake_baseline  # noqa: E402  (local import — keeps this module importable standalone)

#: Once a PR has drifted onto this many distinct new heads since its last
#: approved baseline, every further observation is "ceiling" — blocked,
#: independent of the current head — until a human runs the recovery command
#: (which does not reset this counter; see module docstring).
CEILING = 3

#: How recently the `intake-approved` label must have been applied for a first
#: observation to auto-baseline the head that is live at that moment.
#:
#: 900s = 15 minutes: one cron poll interval (10 min) plus 50% slack, because
#: the gate runs at Step 4 of an iteration rather than at its start. Raising
#: this is not an operational fix — it widens the race window by exactly the
#: amount it is raised. Lowering it costs operator rebaselines and nothing else.
#:
#: A module-level constant on purpose: the value is consumed inside a
#: fail-closed security decision, so reading it from the control plane or an
#: environment variable would buy a knob nobody asked for at the price of a new
#: failure mode ("config unreadable -> what?"). Tests monkeypatch this attribute.
FIRST_OBSERVATION_GRACE_SECONDS = 900


def _first_observation_grace() -> float:
    """Resolve `FIRST_OBSERVATION_GRACE_SECONDS`, or `0` if it is unusable.

    Zero, never the default: a missing attribute, a string, `None`, a bool,
    `NaN`, `inf` or a negative value all mean the configuration is broken, and
    a broken configuration must refuse every first observation rather than
    silently restore a permissive window. Read through `sys.modules` so a test
    that *deletes* the attribute reaches this path too.
    """
    raw = getattr(sys.modules[__name__], "FIRST_OBSERVATION_GRACE_SECONDS", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    if not math.isfinite(raw) or raw < 0:
        return 0.0
    return float(raw)


def parse_labeled_at(value: object) -> Optional[datetime]:
    """Strictly parse a GitHub event `created_at` into an aware datetime.

    Returns `None` for anything that is not an unambiguous, timezone-aware
    instant — junk, an empty string, a non-string, or a naive timestamp whose
    zone we would have to guess. Callers treat `None` as "unreadable", never
    as "now". GitHub emits `%Y-%m-%dT%H:%M:%SZ`; the trailing `Z` is rewritten
    because `datetime.fromisoformat` only accepts it from 3.11 on.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _first_observation_is_fresh(labeled_at: object, now: Optional[datetime]) -> bool:
    """Is *labeled_at* close enough to our own clock to auto-baseline?

    One symmetric `abs(...)` comparison covers both directions: a label too
    far in the past (the state-dir-loss case, and the case where an approval
    has been sitting unobserved) and one too far in the future (a clock
    anomaly). Small forward skew between GitHub's clock and ours is normal and
    stays inside the grace.
    """
    stamped = parse_labeled_at(labeled_at)
    if stamped is None:
        return False
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        return False
    return abs((current - stamped).total_seconds()) <= _first_observation_grace()


def _default_store_path() -> Path:
    """Store lives under the runtime state dir, in its own file — sibling to
    (never shared with) `intake_baseline`'s Discussion-side store. See module
    docstring for why a shared file would be unsafe.

    Falls back to a repo-local dotfile if `state_paths` is unavailable.
    Deliberately narrow `except ImportError` (not `except Exception`) — a
    bare Exception catch here would also swallow
    `state_paths.UnsandboxedStatePathError` (the pytest guard) and silently
    relocate this store to the repo-local fallback, defeating the fail-closed
    property that guard exists to provide. Matches
    `intake_baseline._default_store_path()`'s own shape exactly.
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.state_paths import PR_HEAD_BASELINES, ensure_state_dir  # type: ignore

        ensure_state_dir()
        return Path(PR_HEAD_BASELINES)
    except ImportError:
        return _REPO_ROOT / ".autonomous-team" / "pr-head-baselines.json"


def pr_key(repo_slug: str, pr: int) -> str:
    """Repo-scoped baseline store key: "{owner}/{name}#{number}" — the same
    shape `intake_baseline._validate_key()` requires, and the same shape a
    Discussion key has. Collision with the Discussion store is prevented by
    routing through a different *file* (see module docstring), never by
    making this key look different.
    """
    return f"{repo_slug}#{pr}"


def check_and_record(
    key: str,
    head_sha: str,
    labeled_at: object,
    *,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> str:
    """Map *(key, head_sha, labeled_at)* to a verdict in
    ``{"match", "drifted", "unknown", "ceiling"}`` — never ``"absent"``.

    *labeled_at* is the server-stamped `created_at` of the `labeled` event
    that applied `intake-approved`, and it is **required** rather than
    defaulted: a default would let a future call site inherit the old
    permissive first-observation behaviour silently, whereas a missing
    argument is a `TypeError` at that call site. It is consulted only on a
    first observation; an existing row is compared by SHA alone.

    *now* is a test seam, defaulting to `datetime.now(timezone.utc)`. It is
    not a policy knob and no production caller passes it.

    A row already at or past the ceiling stays "ceiling" even when the
    current head matches the stored baseline (a rebaseline restores the
    stored head without clearing the counter — see module docstring), so the
    ceiling is checked before returning either "match" or "drifted".
    """
    p = path or _default_store_path()

    verdict = intake_baseline.check_baseline(
        key,
        {"content_sha256": head_sha, "last_edited_at": None, "edit_count": 0},
        path=p,
    )

    if verdict == "unknown":
        return "unknown"

    if verdict == "absent":
        # First observation of an approved, labeled PR. Auto-baseline to the
        # current head — this is the fix for the `absent` arm re-opening the
        # bug: should_block_spawn never sees "absent" from this caller.
        #
        # But only when the approval is fresh. Auto-baselining an old label
        # records whatever head is live now as "approved", and the older the
        # label, the less that head has to do with what a human read. Refusing
        # returns "unknown" -> external_pr_head_unrecorded -> blocked, and the
        # operator clears it with rebaseline-pr. Fail closed, never "match".
        if not _first_observation_is_fresh(labeled_at, now):
            return "unknown"
        try:
            intake_baseline.record_baseline(
                key,
                content_sha256=head_sha,
                last_edited_at=None,
                edit_count=0,
                editor=None,
                path=p,
                source="first_observed_pr_approval",
            )
        except Exception:  # noqa: BLE001 — fail closed: an unrecorded baseline is unknown, not a pass
            return "unknown"
        return "match"

    entry = intake_baseline.get_entry(key, path=p)
    already_at_ceiling = entry is not None and entry.get("invalidation_count", 0) >= CEILING

    if verdict == "match":
        return "ceiling" if already_at_ceiling else "match"

    # verdict == "drifted"
    if already_at_ceiling:
        return "ceiling"

    already_dismissed = entry is not None and entry.get("dismissed_content_sha256") == head_sha
    if already_dismissed:
        count = entry.get("invalidation_count", 0)
    else:
        count = intake_baseline.bump_invalidation(key, path=p)
        intake_baseline.mark_dismissed(key, head_sha, path=p)

    return "ceiling" if count >= CEILING else "drifted"


def invalidation_count(key: str, path: Optional[Path] = None) -> Optional[int]:
    """*key*'s stored invalidation count, or `None` when it cannot be read.

    A local store read, zero GitHub calls. `None` covers both "no row" and
    "the store would not read", which callers report as unrecorded rather
    than as a count of zero — the distinction matters because `rebaseline_pr`
    uses this to tell an operator whether the PR they just re-approved is
    still blocked, and guessing zero there would print a reassuring lie.
    """
    p = path or _default_store_path()
    try:
        entry = intake_baseline.get_entry(key, path=p)
    except Exception:  # noqa: BLE001 — an unreadable store is "unknown", not "zero"
        return None
    if not isinstance(entry, dict):
        return None
    count = entry.get("invalidation_count", 0)
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def rebaseline(key: str, head_sha: str, path: Optional[Path] = None) -> None:
    """The recovery operation: re-approve a PR at its *current* head.

    A deliberate local operator action, not something a re-label can trigger
    (the stored row is the only place "already blocked" is remembered, and a
    re-label carries no information about which head it is meant to
    re-approve). Does NOT reset `invalidation_count` — `record_baseline()`
    always carries it forward — so a PR that has hit the ceiling stays at the
    ceiling after this call; only a genuinely fresh row (never before
    recorded) starts the count at zero. See module docstring.
    """
    p = path or _default_store_path()
    intake_baseline.record_baseline(
        key,
        content_sha256=head_sha,
        last_edited_at=None,
        edit_count=0,
        editor=None,
        path=p,
        source="manual_rebaseline",
    )
