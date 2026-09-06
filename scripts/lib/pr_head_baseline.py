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
maps to "not blocked"), so this module auto-baselines a first observation to
the current head and reports `"match"` — the caller (`pr_intake_gate.py`)
therefore only ever receives one of `{"match", "drifted", "unknown", "ceiling"}`.

THIS IS A BOUNDED RACE, NOT A VERIFIED HEAD (D#2421 PR 3)
-----------------------------------------------------------
Read the paragraph above precisely: "auto-baselines a first observation to
the current head" means whatever head is live at the moment the gate first
observes the label — not necessarily the head a human actually reviewed. If
the label lands and the author force-pushes before the next poll, the first
observation records the force-pushed head as "approved" and reports
`"match"`. The window is bounded by the poll interval between the label
landing and this module's first look at the PR, but it is not zero, and
nothing in this module closes it.

Closing it needs a server-stamped timestamp for *when the label was applied*
compared against our own clock, not against the head's `committer.date` —
that field is attacker-controlled (a force-pusher sets it to whatever predates
the label and turns an honest unbaselined pass into a confident false one).
That comparison, and the operational cost it introduces (a state-dir loss
fails every already-approved external PR closed until an operator
re-baselines each one), is D#2421 PR 3's scope, not this module's. Until PR 3
ships, treat this binding as: a force-push is caught on the *next* poll after
the first one (see `check_and_record`'s `"drifted"` / `"ceiling"` outcomes),
never on the first.
"""

from __future__ import annotations

import sys
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


def check_and_record(key: str, head_sha: str, path: Optional[Path] = None) -> str:
    """Map *(key, head_sha)* to a verdict in
    ``{"match", "drifted", "unknown", "ceiling"}`` — never ``"absent"``.

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
