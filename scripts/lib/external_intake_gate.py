"""scripts/lib/external_intake_gate.py — classify Discussion provenance and gate spawns.

Launch-blocking security boundary (D#1588 Batch A): on a public repo, anyone can
open a Discussion or post a comment. This module decides whether a Discussion is
"internal" (authored by a trusted maintainer / the bot itself) or "external"
(everyone else), and whether automation is allowed to act on it.

Trust set (TA deadlock-prevention finding — see D#1588):

    repo push/admin collaborators
    ∪ {boss_github_username, BOT_ACCOUNT}   (bot ALWAYS included, even if
                                              the collaborators API call fails)
    ∪ config.maintainer_allowlist

Every currently-open Discussion in this repo is authored by the configured
bot account (BOT_ACCOUNT — see below), NOT the configured boss. A naive
`author != boss -> external` rule would classify the entire backlog as
external and deadlock the loop — nothing would ever ship. The union above
prevents that while still gating genuinely external authors.

Fail-closed (HG-1): any code path with a missing/empty/None author login, or any
error resolving the allowlist, MUST classify as "external" — never "internal".

Human-only approval (HG-8): no function in this module ever APPLIES the
`intake-approved` label. Only `provenance:internal` / `provenance:external` are
agent-applied. `intake-approved` is read-only from this module's perspective —
a human clicks it in the GitHub UI.

Label authority, not text (HG-3): `should_block_spawn()` only accepts a `labels`
list — the caller must supply labels read from the real GitHub Labels API. This
module never parses Discussion/comment body text for approval tokens.

Content baseline (D#1672, HG-6 real fix): `intake-approved` is bound to the
*content* that was approved, not just the Discussion number. `check_discussion()`
and `classify_and_label()` consult `scripts/lib/intake_baseline.py` on every
call and dismiss (remove) the label automatically the moment a post-approval
edit is observed — content hash, `lastEditedAt`, or `userContentEdits.totalCount`
moving is each independently sufficient (OR, never AND — see intake_baseline.py
module docstring). `should_block_spawn()` itself stays pure: it only reads a
`baseline_verdict` the caller already computed, it never touches the store.

Trust key (D#1840, CWE-290): ``classify_provenance()`` and ``resolve_allowlist()``
below are generic, unchanged, login-agnostic primitives — see
scripts/lib/trust_id_resolver.py's module docstring for why. The actual live
classification path (check_discussion, classify_and_label,
backfill_all_open_discussions) does NOT call them directly; it calls
``resolve_allowlist_ids()`` and compares the author's immutable node ID
(fetched alongside the login in the same GraphQL query — zero net-new round
trips) instead of the login string. A renamed-away login can no longer
inherit trust; an account resolved once and later deleted goes inert rather
than becoming re-registrable. ``resolve_allowlist()``/``classify_provenance()``
stay exactly as they were for backward compatibility with the existing test
suite, which exercises their login-based fail-closed behaviour directly.

CLI:
    python3 scripts/lib/external_intake_gate.py check-discussion <N>
    python3 scripts/lib/external_intake_gate.py classify-and-label <N>
    python3 scripts/lib/external_intake_gate.py backfill
    python3 scripts/lib/external_intake_gate.py setup-labels
    python3 scripts/lib/external_intake_gate.py security-required <N>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake_baseline  # noqa: E402  (local import — keeps this module importable standalone)
import trust_id_resolver  # noqa: E402  (D#1840 — ID-based trust; see its module docstring)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / ".autonomous-team" / "config.json"


def _resolve_bot_account(config: Optional[dict] = None) -> str:
    """Resolve the bot account GitHub login that MUST always be in the trust
    set (TA Risk 1) — even if the collaborators API call fails or returns an
    empty list.

    D#1905: this used to be a hard-coded literal naming this project's own
    bot account. The bot account is a per-adopter identity, not a codebase
    constant — every fork of this project runs its automation as a
    *different* GitHub account. A hard-coded literal here means an
    adopter's own bot gets classified EXTERNAL (their Discussions would
    deadlock the loop exactly as the module docstring above describes)
    while OUR bot stays trusted on THEIR fork — the same confused-deputy
    shape D#1870/#1879 fixed for the repo slug via backend/_repo.py.
    Resolved the same way:

      1. AUTONOMOUS_TEAM_BOT_ACCOUNT environment variable — explicit override.
      2. .autonomous-team/config.json "bot_account" field.
      3. Unset: fail loudly. There is deliberately no hard-coded fallback —
         silently defaulting to this project's own identity is the wrong
         failure mode for a forked adopter (see backend/_repo.py's module
         docstring for the same argument applied to the repo slug).
    """
    from_env = os.environ.get("AUTONOMOUS_TEAM_BOT_ACCOUNT")
    if from_env:
        return from_env

    cfg = config if config is not None else _load_config()
    from_config = cfg.get("bot_account")
    if from_config:
        return from_config

    raise RuntimeError(
        "scripts/lib/external_intake_gate.py: could not resolve BOT_ACCOUNT. "
        "Set the AUTONOMOUS_TEAM_BOT_ACCOUNT environment variable, or add a "
        '"bot_account" field to .autonomous-team/config.json naming the '
        "GitHub account your automation runs as. This account is always "
        "treated as internal/trusted, so it must be configured explicitly — "
        "never inherited from this codebase's own identity."
    )


def _resolve_default_discussion_repo_slug() -> str:
    """Resolve this module's default repo slug — the **Discussion** plane.

    D#1870/#1879: this used to be a hard-coded literal shared by 13 function
    signatures in this module. All six real call sites invoke this module's
    CLI with only a Discussion/PR number (pre-spawn-check.sh, merge-and-
    hook.sh, loop-phased-step5.sh x2, team-lead-iteration.sh, ci-status-
    check.sh) — none of them pass repo_slug explicitly, so the hard-coded
    default WAS live-reachable, not dead code as a since-corrected allowlist
    entry once claimed. On an adopter's fork this module would resolve its
    trust allowlist from OUR push collaborators, authorize THEIR Discussion
    against OUR repo, and write labels/comments to OUR repo with THEIR
    token (CWE-863 / CWE-441 confused deputy / CWE-668). Resolve through
    the same fail-loud path backend/_repo.py uses instead of defaulting to
    a slug the caller may not own.

    D#2348 PR-f2: that fix resolved through ``backend._repo.REPO``, which is
    the *code* plane once code and Discussions live in different repos. Every
    consumer of this constant is Discussion-plane — enumerated below — so it
    now resolves ``DISCUSSION_REPO``. The fourteen consumers, by what they
    actually do rather than by what they are named:

      GraphQL reads/writes against a Discussion node
        fetch_discussion_meta, _get_label_id, apply_provenance_label,
        remove_label, post_discussion_comment, setup_labels,
        backfill_all_open_discussions, _reconcile_baseline

      Composite entry points that pass the slug down to those
        check_discussion, classify_and_label, _security_required_check

      Baseline store key (``"<repo>#<number>"`` namespacing)
        _discussion_key — keys a Discussion, so it keys by the repo the
        Discussion lives in

      Trust set — collaborators(permission=push|admin)
        _fetch_collaborators, resolve_allowlist, resolve_allowlist_ids

    The trust-set three are the only ones that could plausibly read the code
    plane, and they must not. The question they answer is "is the author of
    *this Discussion* a maintainer", and after the cutover the code repo is
    public: push on a public code repo is routinely granted to outside
    contributors who have no standing to drive automation. Keying trust on
    the private Discussion repo's collaborators is the same confused-deputy
    argument D#1879 made, applied across planes instead of across forks.

    There is no code-plane consumer in this module. Nothing here touches a
    PR, a commit or a check run.

    ``DISCUSSION_REPO`` is legitimately "" in a fork with no private twin
    (see backend/_repo_planes.py). Falling back to ``REPO`` there is not a
    hard-coded default and not a fail-open: an empty Discussion plane means
    the checkout was never split, so its Discussions live in the one repo it
    knows about — which is exactly what this module read before, so a fork's
    behaviour is unchanged.
    """
    sys.path.insert(0, str(_REPO_ROOT))
    from backend._repo import DISCUSSION_REPO, REPO  # type: ignore  # noqa: PLC0415

    return DISCUSSION_REPO or REPO


DEFAULT_DISCUSSION_REPO_SLUG = _resolve_default_discussion_repo_slug()

#: Collaborator resolution cache TTL — avoids hammering the GitHub API on every
#: loop iteration / spawn check.
CACHE_TTL_SECONDS = 3600

PROVENANCE_INTERNAL = "internal"
PROVENANCE_EXTERNAL = "external"

INTAKE_APPROVED_LABEL = "intake-approved"


# ---------------------------------------------------------------------------
# Config / cache-path helpers
# ---------------------------------------------------------------------------


def _load_config(config_path: Optional[Path] = None) -> dict:
    path = config_path or _DEFAULT_CONFIG_PATH
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — fail closed: empty config, no extra trust
        return {}


#: The bot account that authors every Discussion this team currently files.
#: MUST always be in the trust set (TA Risk 1) — even if the collaborators API
#: call fails or returns an empty list. Resolved from config/env (D#1905),
#: not hard-coded — see _resolve_bot_account() above.
BOT_ACCOUNT = _resolve_bot_account()


def _default_cache_path() -> Path:
    """Cache lives under the runtime state dir so it survives worktree churn.

    Falls back to a repo-local dotfile if state_paths is unavailable (e.g. when
    this module is imported standalone without the backend package on sys.path).
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.state_paths import STATE_DIR, ensure_state_dir  # type: ignore

        ensure_state_dir()
        return Path(STATE_DIR) / "external_intake_allowlist_cache.json"
    except ImportError:
        # Fallback for environments where state_paths is not on sys.path.
        # Deliberately narrow (not `except Exception`): a bare Exception
        # catch here would also swallow state_paths.UnsandboxedStatePathError
        # (D#1810's PYTEST_CURRENT_TEST guard, a RuntimeError subclass) and
        # silently relocate this cache to the repo-local fallback — defeating
        # the fail-closed property that guard exists to provide. Matches the
        # pattern already used in spec_external_docs.py and
        # intake_baseline.py's _default_store_path().
        return _REPO_ROOT / ".autonomous-team" / ".external-intake-allowlist-cache.json"


def _read_collaborator_cache(cache_path: Path) -> Optional[set]:
    try:
        data = json.loads(cache_path.read_text())
        cached_at = data.get("cached_at", 0)
        if (time.time() - cached_at) < CACHE_TTL_SECONDS:
            return set(data.get("collaborators", []))
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_collaborator_cache(cache_path: Path, collaborators: set) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"cached_at": time.time(), "collaborators": sorted(collaborators)})
        )
    except Exception:  # noqa: BLE001
        pass  # non-fatal — worst case we re-resolve next call


# ---------------------------------------------------------------------------
# Collaborator resolution (the only network call in this module's core path)
# ---------------------------------------------------------------------------


def _fetch_collaborators(repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> set:
    """Return the set of GitHub logins with push or admin permission on *repo_slug*.

    Fail-closed: any error (network, auth, parse) returns an EMPTY set rather than
    raising. Callers must never treat an empty result as "nobody is trusted" —
    resolve_allowlist() always unions in the bot/boss/maintainer_allowlist base
    regardless of what this function returns.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}/collaborators?permission=push", "--paginate"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return set()
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return set()
        logins = set()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            perms = entry.get("permissions") or {}
            if perms.get("push") or perms.get("admin"):
                login = entry.get("login")
                if login:
                    logins.add(login)
        return logins
    except Exception:  # noqa: BLE001
        return set()


def resolve_allowlist(
    config: Optional[dict] = None,
    *,
    repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG,
    cache_path: Optional[Path] = None,
    collaborators_fetcher=None,
    force_refresh: bool = False,
) -> set:
    """Resolve the full trust set.

        collaborators(repo, permission=push|admin) ∪ {boss, BOT_ACCOUNT}
        ∪ config.maintainer_allowlist

    The collaborator lookup is cached ~1h (CACHE_TTL_SECONDS) — cache stores only
    the collaborator portion; the config-derived base is cheap (local file read)
    and always recomputed so a config edit takes effect immediately without
    waiting out the cache TTL.

    Fail-closed: if the collaborators fetch raises or errors, the collaborator
    portion is treated as empty — the bot/boss/maintainer_allowlist base is still
    returned so the loop never deadlocks, but no *additional* trust is granted.
    """
    cfg = config if config is not None else _load_config()
    boss = cfg.get("boss_github_username") or ""
    maintainer_allowlist = set(cfg.get("maintainer_allowlist") or [])

    base = {BOT_ACCOUNT} | maintainer_allowlist
    if boss:
        base.add(boss)

    cpath = cache_path or _default_cache_path()

    collaborators: Optional[set] = None
    if not force_refresh:
        collaborators = _read_collaborator_cache(cpath)

    if collaborators is None:
        fetcher = collaborators_fetcher or _fetch_collaborators
        try:
            collaborators = fetcher(repo_slug)
        except TypeError:
            # Test-double fetchers commonly take no args — retry bare.
            try:
                collaborators = fetcher()
            except Exception:  # noqa: BLE001
                collaborators = set()
        except Exception:  # noqa: BLE001 — fail closed: no extra trust from a broken resolver
            collaborators = set()
        if not isinstance(collaborators, set):
            collaborators = set(collaborators or [])
        _write_collaborator_cache(cpath, collaborators)

    return collaborators | base


# ---------------------------------------------------------------------------
# ID-based trust set (D#1840, CWE-290) — the actual fix. See
# trust_id_resolver.py's module docstring for the full design rationale.
# ---------------------------------------------------------------------------


def _log_loud(message: str) -> None:
    sys.stderr.write(f"[external_intake_gate] {message}\n")


def resolve_allowlist_ids(
    config: Optional[dict] = None,
    *,
    repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG,
    id_cache_path: Optional[Path] = None,
    collaborator_id_fetcher=None,
    trust_store_path: Optional[Path] = None,
    resolver=None,
    force_refresh: bool = False,
) -> set:
    """Resolve the trust set as immutable node IDs — the ID-based counterpart
    to resolve_allowlist() (which stays login-based and unchanged).

    For this repo, ``bot_account_id`` / ``boss_github_user_id`` are pinned in
    config, so the two literals below cost nothing on the hot path — no
    resolver call, just a config read. An adopter who has not pinned them
    yet gets live resolution + the asymmetric availability policy below
    (AC-15), and a successful resolution self-heals the trust store so
    later calls also pay nothing.

    Fail-closed throughout: UNKNOWN or ABSENT never falls back to comparing
    a login string, at any point in this function (R3).
    """
    cfg = config if config is not None else _load_config()
    resolve_fn = resolver or trust_id_resolver.resolve_login_to_id
    ids: set = set()

    # 1. Collaborators — field-selection variant of _fetch_collaborators,
    #    own ~1h cache, zero net-new API calls relative to resolve_allowlist().
    icpath = id_cache_path or trust_id_resolver.default_id_cache_path()
    collaborator_ids: Optional[set] = None
    if not force_refresh:
        collaborator_ids = trust_id_resolver.read_id_cache(icpath)
    if collaborator_ids is None:
        fetcher = collaborator_id_fetcher or trust_id_resolver.fetch_collaborator_ids
        try:
            collaborator_ids = fetcher(repo_slug)
        except Exception:  # noqa: BLE001 — fail closed: no extra trust from a broken fetch
            collaborator_ids = set()
        if not isinstance(collaborator_ids, set):
            collaborator_ids = set(collaborator_ids or [])
        trust_id_resolver.write_id_cache(icpath, collaborator_ids)
    ids |= collaborator_ids

    # 2. Bot — availability-critical (external_intake_gate.py:15-20: every
    #    currently-open Discussion is bot-authored; misclassifying it
    #    deadlocks the loop). ABSENT -> contribute nothing, log loudly.
    #    UNKNOWN -> last-known-good ID from the trust store, log loudly.
    #    Never a login fallback, never persisted as a login-keyed result.
    bot_login = BOT_ACCOUNT
    bot_id = cfg.get("bot_account_id")
    if not bot_id:
        res = resolve_fn(bot_login)
        if res["state"] == trust_id_resolver.RESOLVED:
            bot_id = res["id"]
            trust_id_resolver.record_resolved_id(bot_login, bot_id, path=trust_store_path)
        elif res["state"] == trust_id_resolver.UNKNOWN:
            bot_id = trust_id_resolver.get_stored_id(bot_login, path=trust_store_path)
            if bot_id:
                _log_loud(
                    f"bot_account id resolution unknown for {bot_login!r} — "
                    "using last-known-good id from the trust store"
                )
            else:
                _log_loud(
                    f"bot_account id resolution unknown for {bot_login!r} and no "
                    "last-known-good id is recorded — bot contributes no id this call"
                )
        else:  # ABSENT
            bot_id = None
            _log_loud(
                f"bot_account login {bot_login!r} no longer resolves to any GitHub "
                "account — dropped from the trust set (never a login fallback)"
            )
    if bot_id:
        ids.add(bot_id)

    # 3. Boss — NOT availability-critical (HG-8: a misclassified boss
    #    Discussion just needs a human to click intake-approved). Fail
    #    closed, no degradation: ABSENT or UNKNOWN both contribute nothing.
    boss_login = cfg.get("boss_github_username") or ""
    boss_id = cfg.get("boss_github_user_id")
    if boss_login and not boss_id:
        res = resolve_fn(boss_login)
        if res["state"] == trust_id_resolver.RESOLVED:
            boss_id = res["id"]
            trust_id_resolver.record_resolved_id(boss_login, boss_id, path=trust_store_path)
        else:
            boss_id = None
    if boss_id:
        ids.add(boss_id)

    # 4. maintainer_allowlist — same fail-closed-as-boss policy per entry.
    #    Entries equal to bot_login are skipped (already covered by step 2,
    #    and are the measured common case — this repo's own maintainer_
    #    allowlist duplicates bot_account exactly).
    for login in cfg.get("maintainer_allowlist") or []:
        if login == bot_login:
            continue
        res = resolve_fn(login)
        if res["state"] == trust_id_resolver.RESOLVED:
            trust_id_resolver.record_resolved_id(login, res["id"], path=trust_store_path)
            ids.add(res["id"])
        # ABSENT/UNKNOWN -> contribute nothing for this entry (fail closed).

    return ids


# ---------------------------------------------------------------------------
# Pure classification (HG-1 fail-closed)
# ---------------------------------------------------------------------------


def classify_provenance(author_login: Optional[str], allowlist: set) -> str:
    """Classify a Discussion's provenance from its author login against *allowlist*.

    Fail-closed: empty/None author, or any error, is ALWAYS "external" — never
    "internal". This is the load-bearing security invariant of the whole gate.

    Generic on purpose (D#1840): this is a plain "is *author_login* a member
    of *allowlist*" fail-closed check — it does not care whether the values
    are logins or immutable node IDs. The live classification path
    (check_discussion, classify_and_label, backfill_all_open_discussions)
    calls this with an author node ID and resolve_allowlist_ids()'s ID set;
    the existing tests in backend/tests/test_external_intake_gate.py call it
    directly with logins and resolve_allowlist()'s login set. Both are valid
    uses of the same fail-closed primitive — do not special-case either
    representation here.
    """
    if not author_login:
        return PROVENANCE_EXTERNAL
    try:
        is_trusted = author_login in allowlist
    except Exception:  # noqa: BLE001
        return PROVENANCE_EXTERNAL
    return PROVENANCE_INTERNAL if is_trusted else PROVENANCE_EXTERNAL


def should_block_spawn(
    author_login: Optional[str],
    labels: Optional[list],
    allowlist: set,
    *,
    baseline_verdict: Optional[str] = None,
) -> tuple[bool, str]:
    """Decide whether automation may act on a Discussion.

    *labels* MUST be the real label list read from the GitHub Labels API — this
    function never inspects body/comment text (HG-3, the `[team-lead-signed]`
    forgery lesson). A Discussion whose body merely CONTAINS the string
    "intake-approved" is not approved; only a real `intake-approved` label is.

    *baseline_verdict* (D#1672, keyword-only, defaults to None for backward
    compatibility with every existing 3-arg call site) is the already-computed
    result of `intake_baseline.check_baseline()` — this function stays PURE and
    never reads/writes the baseline store itself; the caller (check_discussion,
    classify_and_label) owns that I/O. Verdict table:

        None / "match" / "absent"  -> (False, "external_approved")
        "drifted"                  -> (True,  "external_edited_after_approval")
        "unknown" / anything else  -> (True,  "external_baseline_unreadable")  (fail closed, HG-1)

    Returns (blocked: bool, reason: str).
    """
    provenance = classify_provenance(author_login, allowlist)
    if provenance == PROVENANCE_INTERNAL:
        return False, "internal"

    label_names = set(labels or [])
    if INTAKE_APPROVED_LABEL not in label_names:
        return True, "external_awaiting_intake_approval"

    if baseline_verdict in (None, "match", "absent"):
        return False, "external_approved"
    if baseline_verdict == "drifted":
        return True, "external_edited_after_approval"
    # "unknown", or any value this function doesn't recognise — fail closed.
    return True, "external_baseline_unreadable"


# ---------------------------------------------------------------------------
# Untrusted-content isolation (HG-5)
# ---------------------------------------------------------------------------

UNTRUSTED_DELIMITER_START = "<<UNTRUSTED EXTERNAL CONTENT>>"
UNTRUSTED_DELIMITER_END = "<<END UNTRUSTED>>"


def sanitize_and_delimit_external(body: str) -> str:
    """Strip control-plane tokens from *body* then wrap it in an explicit
    untrusted-content delimiter, for embedding into any PM/reviewer/executor
    prompt that includes external-provenance content.

    Reuses route_discussion_wiring.sanitize_body() so the same control-token
    stripping (SPAWN_REQUEST / TERMINATE_REQUEST / STATUS: / HTML comments /
    fake AGENT_OUTPUT blocks) applies here too — no duplicate denylist to drift.

    The delimiters are neutralized inside the body before wrapping (D#2348
    PR-k). sanitize_body()'s denylist covers control tokens but never covered
    the fence itself, so a body containing the literal close delimiter ended
    the fence early and could then forge a trusted-looking section after it —
    the exact escape the fence exists to prevent, arriving through the fence.
    Neutralizing is provably closed rather than merely one pass: neither
    replacement string contains either delimiter, and no delimiter can be
    reconstructed across a replacement boundary, because every prefix of a
    replacement begins "<<" while every suffix of a delimiter ends ">>".
    """
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    from route_discussion_wiring import sanitize_body  # noqa: E402  (local import — keeps this module importable standalone)

    sanitized = sanitize_body(body)
    sanitized = sanitized.replace(UNTRUSTED_DELIMITER_END, "<<END UNTRUSTED (neutralized)>>")
    sanitized = sanitized.replace(UNTRUSTED_DELIMITER_START, "<<UNTRUSTED EXTERNAL CONTENT (neutralized)>>")
    return f"{UNTRUSTED_DELIMITER_START}\n{sanitized}\n{UNTRUSTED_DELIMITER_END}"


# ---------------------------------------------------------------------------
# External-provenance forces mandatory security review (HG-7, D#1588 Batch B)
# ---------------------------------------------------------------------------


def external_provenance_forces_security_review(labels: Optional[list]) -> bool:
    """Return True when *labels* (the ORIGINATING DISCUSSION's real label list,
    NOT the PR's) contains ``provenance:external``.

    HG-7: a PR that traces back to a provenance:external Discussion must treat
    security-passed as a hard merge-gate requirement — even a trivial-looking
    diff — and is NOT eligible for the Team-Lead direct-merge exception
    (CLAUDE.md "Merge Gate Protocol"). This is independent of, and additive
    to, the content-based security trigger (`scripts/lib/security-trigger.sh`)
    that scans the diff itself: an external-provenance origin forces the
    requirement even when the diff trips no keyword/file trigger.

    Extends D#1537's per-fork-PR CI rule to the Discussion-provenance
    dimension. Pure function — callers pass the label list they already read
    from the GitHub Labels API (see `fetch_discussion_meta`); this function
    never fetches anything itself.
    """
    return "provenance:external" in set(labels or [])


# ---------------------------------------------------------------------------
# Live GitHub lookups — Discussion author/labels, label ids, mutations
# ---------------------------------------------------------------------------


def _gh_graphql(args: list) -> Optional[dict]:
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:  # noqa: BLE001
        return None


_META_FAILURE_SHAPE = {
    "id": None,
    "author": None,
    "author_id": None,
    "labels": [],
    "fetch_ok": False,
    "title": None,
    "body": None,
    "last_edited_at": None,
    "editor": None,
    "edit_count": 0,
}


def fetch_discussion_meta(number: int, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> dict:
    """Fetch a Discussion's id, author, labels, and content-baseline fields
    (live, uncached) — a single GraphQL round-trip (performance-expert, D#1672:
    zero net-new round-trips — the baseline fields ride the existing query).

    Returns {"id", "author", "author_id", "labels", "fetch_ok", "title",
    "body", "last_edited_at", "editor", "edit_count"}.

    ``author_id`` (D#1840) is the author's immutable GraphQL node ID, added
    via a ``... on User{id}`` fragment on the same query — GraphQL's
    ``author`` field is typed as the ``Actor`` interface, so this fragment
    matches a human User but NOT a Bot-type author (a GitHub App); a
    Bot-authored Discussion therefore always has ``author_id`` None. That is
    intentional fail-closed behaviour (TA-4) — the ID-based classification
    path in resolve_allowlist_ids()/classify_provenance() never falls back
    to the login for a missing author_id, so a Bot-type author classifies
    external rather than silently matching on login.

    ``fetch_ok`` is the load-bearing field for HG-7 fail-closed semantics
    (D#1588 Batch B fix) AND for D#1672's baseline fail-open trap: it is True
    only when the GitHub API call itself succeeded and returned parseable
    data — including the case where the Discussion genuinely has zero labels
    or has never been edited. It is False on any network failure, rate limit,
    malformed response, or other fetch error, so callers can tell "confirmed"
    apart from "we don't actually know." On fetch failure every other field
    stays at the documented empty/None default — critically, ``edit_count``
    stays 0 and ``labels`` stays [] on failure exactly the same as they would
    on a genuinely unedited/unlabelled Discussion, which is precisely why
    callers MUST branch on ``fetch_ok`` and never infer failure from an empty
    list (AC-5 / the HG-7 Batch B bug class recurring on this new axis).
    """
    owner, name = repo_slug.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$num:Int!){ "
        "repository(owner:$owner,name:$name){ discussion(number:$num){ "
        "id title body author{login ... on User{id}} lastEditedAt editor{login} "
        "userContentEdits(first:1){totalCount} "
        "labels(first:20){nodes{name}} } } }"
    )
    data = _gh_graphql(
        [
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"num={number}",
        ]
    )
    if not data:
        return dict(_META_FAILURE_SHAPE)
    try:
        disc = (data.get("data") or {}).get("repository", {}).get("discussion") or {}
        author_node = disc.get("author") or {}
        author = author_node.get("login")
        author_id = author_node.get("id")  # None for a Bot-type author — see docstring
        labels = [n["name"] for n in (disc.get("labels") or {}).get("nodes", [])]
        edits = disc.get("userContentEdits") or {}
        return {
            "id": disc.get("id"),
            "author": author,
            "author_id": author_id,
            "labels": labels,
            "fetch_ok": True,
            "title": disc.get("title"),
            "body": disc.get("body"),
            "last_edited_at": disc.get("lastEditedAt"),
            "editor": (disc.get("editor") or {}).get("login"),
            "edit_count": edits.get("totalCount", 0),
        }
    except (KeyError, TypeError, AttributeError):
        return dict(_META_FAILURE_SHAPE)


def _get_label_id(label_name: str, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> Optional[str]:
    owner, name = repo_slug.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$label:String!){ "
        "repository(owner:$owner,name:$name){ label(name:$label){ id } } }"
    )
    data = _gh_graphql(
        [
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-f", f"label={label_name}",
        ]
    )
    if not data:
        return None
    try:
        return (((data.get("data") or {}).get("repository") or {}).get("label") or {}).get("id")
    except (KeyError, TypeError, AttributeError):
        return None


def apply_provenance_label(
    discussion_id: str, provenance: str, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG
) -> bool:
    """Apply exactly one of provenance:internal / provenance:external via the
    GraphQL addLabelsToLabelable mutation (REST issues/N/labels silently no-ops
    on Discussions — confirmed by the D#1588 panel; see import-epic-tasks.py for
    prior art of this exact mutation pattern).

    NEVER applies `intake-approved` (HG-8) — that label is human-only, and this
    function's *provenance* argument is validated against a fixed allow-list of
    the two provenance values so a bad caller cannot smuggle it through here.
    """
    if provenance not in (PROVENANCE_INTERNAL, PROVENANCE_EXTERNAL):
        return False

    label_name = f"provenance:{provenance}"
    label_id = _get_label_id(label_name, repo_slug)
    if not label_id or not discussion_id:
        return False

    mutation = (
        "mutation { addLabelsToLabelable(input: { "
        f'labelableId: "{discussion_id}", labelIds: ["{label_id}"] '
        "}) { labelable { ... on Discussion { number } } } }"
    )
    data = _gh_graphql(["-f", f"query={mutation}"])
    return data is not None and "errors" not in data


def remove_label(discussion_id: str, label_name: str, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> bool:
    """Remove *label_name* from a Discussion via removeLabelsFromLabelable.

    HG-8a carve-out (D#1672, R2): this is the ONLY label-removal path in this
    module, and it is hard-coded to accept exactly `intake-approved` — any
    other label name returns False without making a network call, mirroring
    apply_provenance_label()'s fixed allow-list validation, inverted. HG-8
    itself is untouched: this module still never APPLIES intake-approved.
    Removal cannot forge presence; it is monotonically trust-reducing.

    Uses parameterized GraphQL variables (H-1, D#1672 round 2 security
    review), matching post_discussion_comment() below rather than the
    f-string-interpolated mutation this originally mirrored from
    apply_provenance_label(). Not exploitable today — discussion_id and
    label_id are both GitHub-issued node IDs, never user-controlled text —
    but there is no reason for the new code in this module to carry the
    injection-shaped pattern forward.
    """
    if label_name != INTAKE_APPROVED_LABEL:
        return False

    label_id = _get_label_id(label_name, repo_slug)
    if not label_id or not discussion_id:
        return False

    mutation = (
        "mutation($id:ID!,$labelId:ID!){ removeLabelsFromLabelable(input: { "
        "labelableId: $id, labelIds: [$labelId] "
        "}) { labelable { ... on Discussion { number } } } }"
    )
    data = _gh_graphql(
        ["-f", f"query={mutation}", "-f", f"id={discussion_id}", "-f", f"labelId={label_id}"]
    )
    return data is not None and "errors" not in data


def post_discussion_comment(discussion_id: str, body: str, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> bool:
    """Post *body* as a comment on a Discussion via addDiscussionComment.

    Used only for the dismissal notice (AC-14) — `addDiscussionComment` is on
    the sandbox's fixed GraphQL mutation allow-list (hooks/sandbox_rules.py).
    """
    if not discussion_id:
        return False
    mutation = (
        "mutation($id:ID!,$body:String!){ "
        "addDiscussionComment(input:{discussionId:$id, body:$body}) { comment { id } } }"
    )
    data = _gh_graphql(
        ["-f", f"query={mutation}", "-f", f"id={discussion_id}", "-f", f"body={body}"]
    )
    return data is not None and "errors" not in data


def setup_labels(repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> None:
    """One-time (idempotent) label setup: provenance:internal, provenance:external,
    intake-approved. Safe to re-run — `gh label create --force` updates in place.
    """
    for name, color, desc in (
        ("provenance:internal", "0e8a16", "Discussion authored by a trusted maintainer or the bot"),
        ("provenance:external", "d93f0b", "Discussion authored outside the trust set - inert until intake-approved"),
        (INTAKE_APPROVED_LABEL, "1d76db", "Human maintainer approval to act on an external-provenance Discussion"),
    ):
        subprocess.run(
            [
                "gh", "label", "create", name,
                "--color", color,
                "--description", desc,
                "--repo", repo_slug,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )


def backfill_all_open_discussions(repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> dict:
    """Classify every currently-open Discussion that lacks a provenance:* label
    and apply provenance:internal / provenance:external accordingly.

    No grandfather clause on blocking — a Discussion classified external here
    still requires intake-approved before automation acts on it going forward.
    Never touches Discussions that already carry a provenance label (idempotent).
    """
    owner, name = repo_slug.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$cursor:String){ "
        "repository(owner:$owner,name:$name){ discussions(first:50, after:$cursor, states:[OPEN]){ "
        "pageInfo{hasNextPage endCursor} "
        "nodes{ id number author{login ... on User{id}} labels(first:20){nodes{name}} } } } }"
    )
    allowlist_ids = resolve_allowlist_ids(repo_slug=repo_slug)
    processed: list[dict] = []
    cursor: Optional[str] = None

    while True:
        args = ["-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = _gh_graphql(args)
        if not data:
            break
        disc_conn = (((data.get("data") or {}).get("repository") or {}).get("discussions")) or {}
        for node in disc_conn.get("nodes", []):
            labels = [n["name"] for n in (node.get("labels") or {}).get("nodes", [])]
            if any(l.startswith("provenance:") for l in labels):
                continue
            author_node = node.get("author") or {}
            author = author_node.get("login")
            author_id = author_node.get("id")  # None for a Bot-type author — TA-4, fail closed
            provenance = classify_provenance(author_id, allowlist_ids)
            applied = apply_provenance_label(node.get("id"), provenance, repo_slug)
            processed.append(
                {"number": node.get("number"), "author": author, "provenance": provenance, "applied": applied}
            )
        page_info = disc_conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
        else:
            break

    return {"processed": processed, "count": len(processed)}


# ---------------------------------------------------------------------------
# Content baseline reconciliation (D#1672, HG-6 real fix)
# ---------------------------------------------------------------------------


def _discussion_key(number: int, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> str:
    """Repo-scoped baseline store key: "{owner}/{name}#{number}" (R9, AC-8)."""
    return f"{repo_slug}#{number}"


def _content_hash_from_meta(meta: dict) -> str:
    return intake_baseline.content_hash(meta.get("title") or "", meta.get("body") or "")


def _resolve_baseline_verdict(meta: dict, key: str, path: Optional[Path] = None) -> str:
    """Pure, read-only baseline verdict from already-fetched *meta*.

    Never mutates the store — used both to feed should_block_spawn() and by
    the read-only `security-required` merge-gate check (AC-20), which must
    never have a side effect on the store as part of a merge decision.

    AC-4 (TA's fetch_ok fail-open hazard): a failed fetch, or a response whose
    body is None, resolves to "unknown" BEFORE anything else is inspected —
    never inferred from author/label state, which can look perfectly normal
    on a failed fetch (fetch_discussion_meta's failure shape still returns
    labels=[] / edit_count=0, indistinguishable from a genuinely unedited,
    unlabelled Discussion unless fetch_ok is checked explicitly first).
    """
    if not meta.get("fetch_ok", False) or meta.get("body") is None:
        return "unknown"
    current = {
        "content_sha256": _content_hash_from_meta(meta),
        "last_edited_at": meta.get("last_edited_at"),
        "edit_count": meta.get("edit_count", 0),
    }
    return intake_baseline.check_baseline(key, current, path=path)


def _audit_baseline_transition(key: str, editor: Optional[str], kind: str) -> None:
    """Best-effort audit emit — never raises (SEC-5, AC-24). Names the
    discussion key, the editor login, and the transition kind, per AC-24.
    """
    try:
        from backend.audit_trail import get_audit_trail  # noqa: PLC0415

        get_audit_trail().emit("gate", "transition", key, None, kind, editor or "unknown")
    except Exception:  # noqa: BLE001
        pass


def _build_dismissal_comment(
    meta: dict, previous_entry: Optional[dict], current_hash: str, invalidation_count: int
) -> str:
    """Human-facing dismissal notice (AC-14). Vocabulary is product-owner's:
    no "baseline", "TOCTOU", or "hash" — those stay in code and the wiki only.
    """
    editor = meta.get("editor") or "unknown"
    edited_at = meta.get("last_edited_at") or "an unknown time"
    prev = previous_entry or {}
    old_signature = (prev.get("content_sha256") or "")[:12] or "none recorded"
    new_signature = current_hash[:12]
    old_edited_at = prev.get("observed_last_edited_at") or "never"
    old_edit_count = prev.get("observed_edit_count", 0)
    new_edit_count = meta.get("edit_count", 0)
    times = "time" if invalidation_count == 1 else "times"

    return "\n".join(
        [
            "Approval applies to the description that was reviewed. Editing the "
            "description dismisses it. Wait for the `intake-approved` chip to "
            "disappear before re-adding it -- if it disappears again with no "
            "new comment here, that re-approval was too early: wait longer, "
            "then re-add it.",
            "",
            "What changed:",
            f"- content signature: `{old_signature}` -> `{new_signature}`",
            f"- last edited: {old_edited_at} -> {edited_at}",
            f"- edit count: {old_edit_count} -> {new_edit_count}",
            "",
            f"Edited by @{editor} at {edited_at}.",
            f"Approval has now been dismissed {invalidation_count} {times} on this Discussion.",
        ]
    )


def _reconcile_baseline(
    meta: dict, key: str, discussion_id: Optional[str], repo_slug: str, path: Optional[Path] = None
) -> dict:
    """The ONLY place baseline writes happen — should_block_spawn() stays pure
    (that purity is what keeps the existing test suite and the HG-3
    label-authority argument holding). Called from check_discussion() and
    classify_and_label() — the two loop chokepoints — never from the
    `security-required` CLI (that path is read-only, see
    _resolve_baseline_verdict()).

    Re-approval needs no new API support (performance-expert): label absent at
    observation -> drop the row; label present with no row -> record it.
    Combined with HG-8a's bot-removal, the whole cycle is automatic.

    Note (D#1672 round 2, SEC-1/SEC-3 fix): "label absent at observation ->
    drop the row" is intentionally the ONLY place a dismissed row gets
    dropped now — a "drifted" verdict no longer drops its own row inline.
    That confirmation has to come from a live re-fetch showing the label is
    genuinely gone, not from assuming remove_label()'s mutation succeeded
    synchronously. See the drifted branch below for the full reasoning.
    """
    verdict = _resolve_baseline_verdict(meta, key, path=path)
    if verdict == "unknown":
        return {"verdict": "unknown", "action": "none"}

    has_label = INTAKE_APPROVED_LABEL in set(meta.get("labels") or [])

    if not has_label:
        dropped = intake_baseline.drop_baseline(key, path=path)
        if dropped:
            _audit_baseline_transition(key, meta.get("editor"), "dropped")
        return {"verdict": verdict, "action": "dropped" if dropped else "none"}

    if verdict == "absent":
        intake_baseline.record_baseline(
            key,
            content_sha256=_content_hash_from_meta(meta),
            last_edited_at=meta.get("last_edited_at"),
            edit_count=meta.get("edit_count", 0),
            editor=meta.get("editor"),
            path=path,
        )
        _audit_baseline_transition(key, meta.get("editor"), "recorded")
        return {"verdict": verdict, "action": "recorded"}

    if verdict == "match":
        return {"verdict": verdict, "action": "none"}

    # drifted — dismiss. SEC-1/SEC-3 fix (D#1672 round 2, security review):
    # this branch used to unconditionally drop the row after attempting
    # removal, regardless of whether remove_label() actually succeeded. A
    # transient `gh` failure then left the Discussion as "label present, no
    # row" — which check_baseline() reports as "absent" — so the VERY NEXT
    # observation recorded a fresh baseline against the edited content and
    # should_block_spawn() returned external_approved: one flaky API call
    # silently auto-approved attacker-edited content, with no human involved
    # and no second comment to notice (SEC-1). It also meant the R6/AC-20
    # merge-gate re-check was reading a row that this same reconcile pass had
    # already deleted, so by the time Step-5 merging ran later in the same
    # loop iteration the drifted window had usually already closed (SEC-3).
    #
    # Fix: the row is NOT dropped here. It is dropped later, only once a
    # SUBSEQUENT reconcile pass observes the label is genuinely absent (the
    # `not has_label` branch above) — i.e. only after removal is confirmed
    # live, not assumed. That means:
    #   - removal succeeds -> next pass sees label absent -> row dropped ->
    #     re-approval works exactly as before.
    #   - removal fails -> row (and its "drifted" verdict) survives -> still
    #     blocked, removal retried next pass, merge-gate re-check still sees
    #     "drifted" and still returns rc=4 for as long as the row survives.
    #
    # Retrying removal every pass means we'd otherwise re-post the comment
    # and re-bump invalidation_count on every retry too, for content that
    # hasn't changed again. dismissed_content_sha256 on the row is the dedup
    # marker: only post/bump the FIRST time we observe this exact content
    # hash as drifted; every later pass with the same hash just retries the
    # label removal in silence.
    previous_entry = intake_baseline.get_entry(key, path=path)
    current_hash = _content_hash_from_meta(meta)
    already_dismissed = (
        previous_entry is not None
        and previous_entry.get("dismissed_content_sha256") == current_hash
    )

    if already_dismissed:
        count = previous_entry.get("invalidation_count", 0)
        posted = None
        action = "retry_removal"
    else:
        count = intake_baseline.bump_invalidation(key, path=path)
        comment_body = _build_dismissal_comment(meta, previous_entry, current_hash, count)
        posted = post_discussion_comment(discussion_id, comment_body, repo_slug)
        intake_baseline.mark_dismissed(key, current_hash, path=path)
        _audit_baseline_transition(key, meta.get("editor"), "dismissed")
        action = "dismissed"

    removed = remove_label(discussion_id, INTAKE_APPROVED_LABEL, repo_slug)
    return {
        "verdict": "drifted",
        "action": action,
        "label_removed": removed,
        "comment_posted": posted,
        "invalidation_count": count,
    }


def check_discussion(number: int, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> dict:
    """Live gate check for a single Discussion — used by pre-spawn-check.sh and
    the loop Discussion scan. Always re-derives provenance from live author
    identity at decision time (labels are audit-only) to close the scan-lag
    fail-open window (D#1588 panel Risk 3).
    """
    meta = fetch_discussion_meta(number, repo_slug)
    allowlist_ids = resolve_allowlist_ids(repo_slug=repo_slug)
    provenance = classify_provenance(meta.get("author_id"), allowlist_ids)

    baseline_verdict = None
    if provenance == PROVENANCE_EXTERNAL:
        key = _discussion_key(number, repo_slug)
        transition = _reconcile_baseline(meta, key, meta.get("id"), repo_slug)
        baseline_verdict = transition["verdict"]

    blocked, reason = should_block_spawn(
        meta.get("author_id"), meta.get("labels", []), allowlist_ids, baseline_verdict=baseline_verdict
    )
    return {
        "discussion": number,
        "author": meta.get("author"),
        "provenance": provenance,
        "blocked": blocked,
        "reason": reason,
    }


def classify_and_label(number: int, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> dict:
    """Combined loop-chokepoint call: fetch live Discussion meta, apply exactly
    one of provenance:internal / provenance:external if not already present
    (idempotent — never touches a Discussion that already carries a
    provenance:* label), reconcile the content baseline, then return the same
    shape as check_discussion().

    Used by the Team Lead Step-3 Discussion scan so one GraphQL round-trip
    covers both the audit label write and the spawn-gating decision.
    """
    meta = fetch_discussion_meta(number, repo_slug)
    allowlist_ids = resolve_allowlist_ids(repo_slug=repo_slug)
    provenance = classify_provenance(meta.get("author_id"), allowlist_ids)
    existing_labels = meta.get("labels") or []

    labeled = False
    if not any(l.startswith("provenance:") for l in existing_labels):
        if meta.get("id"):
            labeled = apply_provenance_label(meta["id"], provenance, repo_slug)
            if labeled:
                existing_labels = [*existing_labels, f"provenance:{provenance}"]
                meta = {**meta, "labels": existing_labels}

    baseline_verdict = None
    if provenance == PROVENANCE_EXTERNAL:
        key = _discussion_key(number, repo_slug)
        transition = _reconcile_baseline(meta, key, meta.get("id"), repo_slug)
        baseline_verdict = transition["verdict"]

    blocked, reason = should_block_spawn(
        meta.get("author_id"), existing_labels, allowlist_ids, baseline_verdict=baseline_verdict
    )
    return {
        "discussion": number,
        "author": meta.get("author"),
        "provenance": provenance,
        "labeled": labeled,
        "blocked": blocked,
        "reason": reason,
    }


def _security_required_check(number: int, repo_slug: str = DEFAULT_DISCUSSION_REPO_SLUG) -> tuple[str, int]:
    """Core logic behind the `security-required` CLI command — split out so it
    is unit-testable without a subprocess/gh stub. Returns (stdout_line, exit_code).

    Exit codes:
      0 = required (provenance:external label confirmed present)
      1 = confirmed NOT required (fetch succeeded, label confirmed absent)
      3 = unknown/fetch failed (fail-closed, HG-1)
      4 = required AND the Discussion's approval has been dismissed by a
          post-approval edit since (R6, D#1672) — the merge-gate re-check.
          This is read-only: it consults _resolve_baseline_verdict(), never
          _reconcile_baseline(), so a merge-time check never mutates the
          store or touches GitHub labels/comments as a side effect.
          An *un*-updated caller that only checks `rc == 1` for "not required"
          falls into its existing `else -> required` branch for rc=4 too, so
          this is backward-safe by construction (AC-20).
    """
    meta = fetch_discussion_meta(number, repo_slug)
    if not meta.get("fetch_ok", False):
        return "unknown", 3

    required = external_provenance_forces_security_review(meta.get("labels", []))
    if required:
        key = _discussion_key(number, repo_slug)
        baseline_verdict = _resolve_baseline_verdict(meta, key)
        if baseline_verdict == "drifted":
            return "drifted", 4

    return ("true" if required else "false"), (0 if required else 1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage:\n"
            "  python3 scripts/lib/external_intake_gate.py check-discussion <N>\n"
            "  python3 scripts/lib/external_intake_gate.py classify-and-label <N>\n"
            "  python3 scripts/lib/external_intake_gate.py backfill\n"
            "  python3 scripts/lib/external_intake_gate.py setup-labels\n"
            "  python3 scripts/lib/external_intake_gate.py security-required <N>\n"
            "  cat body.txt | python3 scripts/lib/external_intake_gate.py sanitize\n"
        )
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "check-discussion":
        if len(sys.argv) < 3:
            sys.stderr.write("check-discussion requires a discussion number\n")
            sys.exit(2)
        result = check_discussion(int(sys.argv[2]))
        print(json.dumps(result))
        sys.exit(1 if result["blocked"] else 0)

    elif cmd == "classify-and-label":
        if len(sys.argv) < 3:
            sys.stderr.write("classify-and-label requires a discussion number\n")
            sys.exit(2)
        result = classify_and_label(int(sys.argv[2]))
        print(json.dumps(result))
        sys.exit(1 if result["blocked"] else 0)

    elif cmd == "backfill":
        result = backfill_all_open_discussions()
        print(json.dumps(result, indent=2))

    elif cmd == "setup-labels":
        setup_labels()
        print("labels ensured: provenance:internal, provenance:external, intake-approved")

    elif cmd == "security-required":
        # HG-7 (Batch B): print "true"/"false"/"unknown"/"drifted" and exit
        # 0/1/3/4 — merge-gate scripts (loop-phased-step5.sh, merge-and-hook.sh)
        # call this to decide whether security-passed is a hard requirement for
        # the PR's originating Discussion, and (D#1672) whether the originating
        # Discussion's approval has been dismissed since. See
        # _security_required_check() for the exit-code contract.
        if len(sys.argv) < 3:
            sys.stderr.write("security-required requires a discussion number\n")
            sys.exit(2)
        line, code = _security_required_check(int(sys.argv[2]))
        print(line)
        sys.exit(code)

    elif cmd == "sanitize":
        # HG-5 (Batch B): read untrusted body/comment text from stdin, print the
        # sanitized + delimiter-wrapped result to stdout. Callers (PM prompt
        # assembly) pipe any external-provenance or non-allowlisted-comment
        # text through this before it is ever quoted into an LLM prompt.
        raw = sys.stdin.read()
        print(sanitize_and_delimit_external(raw))

    else:
        sys.stderr.write(f"Unknown command: {cmd}\n")
        sys.exit(2)
