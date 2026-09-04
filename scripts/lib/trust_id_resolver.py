"""scripts/lib/trust_id_resolver.py — resolve GitHub logins to immutable node
IDs, and pin trust to those IDs instead of a mutable login string (D#1840,
CWE-290).

Why this module exists, and why it is separate from external_intake_gate.py:

    ``classify_provenance()`` in external_intake_gate.py keys trust on a
    login string. GitHub releases a username for reuse when an account is
    renamed or deleted — an attacker who registers a freed, still-allowlisted
    login inherits whatever that login was trusted for. This module resolves
    the two config trust literals (``bot_account``, ``boss_github_username``)
    plus any ``maintainer_allowlist`` entries to their immutable GraphQL node
    IDs, so the actual comparison (in ``external_intake_gate.py``) can key on
    something GitHub does not let anyone re-register.

    ``classify_provenance()`` and ``resolve_allowlist()`` themselves are
    deliberately UNCHANGED by this work — they are generic "is this value a
    member of this set" fail-closed primitives, agnostic to whether the
    values are logins or IDs, and backend/tests/test_external_intake_gate.py
    depends on their exact current behaviour (see that file's TestFailClosed
    class, R7). The fix lives in what gets built and passed to them: this
    module supplies an ID-based allowlist and an ID-based author identity;
    ``external_intake_gate.py``'s production call sites (check_discussion,
    classify_and_label, backfill_all_open_discussions) are rewired to use
    them instead of the login-based path.

Three-state resolution (R1/R2, D#1840 panel):

    A login -> ID lookup can land in exactly three places, and they are NOT
    interchangeable:

      RESOLVED  the account exists and its ID is known.
      ABSENT    the account authoritatively does not exist (GraphQL replied
                HTTP 200 with a typed ``errors[].type == "NOT_FOUND"``). This
                is the state an attacker triggers by registering a freed
                login — the entry must go permanently inert.
      UNKNOWN   the lookup could not be completed (network/auth/rate-limit/
                unparseable response). This must NEVER be treated the same
                as ABSENT — doing so would let a transient API failure drop
                a trusted principal (most dangerously the bot account, see
                external_intake_gate.py:15-20 on why that deadlocks the
                loop), and must never fall back to comparing the login
                string either — that would silently reopen the exact
                vulnerability this module exists to close.

    ``gh api graphql`` conflates ABSENT and UNKNOWN at the *exit code* layer
    — it exits 1 on both a typed NOT_FOUND error (HTTP 200) and a genuine
    transport failure. The distinction lives only in stdout, so
    ``resolve_login_to_id()`` parses stdout regardless of exit code — see
    its docstring.

Trust store vs. cache (R4): the resolved-ID store persisted by this module
is NOT a performance cache. It is written only from a fully successful
resolution, is read back for the availability-critical last-known-good
fallback (AC-15), and a corrupt/unparseable store reads as UNKNOWN — never
as "empty" (an empty *collaborator* cache is safe, since it only withholds
extra trust; an empty *trust* store would be read as "nothing was ever
resolved" and could drop the bot's last-known-good ID).

For THIS repo, both literals are pinned directly in .autonomous-team/config.json
(``bot_account_id`` / ``boss_github_user_id``) so the classification hot path
reads them for free and never calls this module's resolver at all — see
``resolve_allowlist_ids()``. The live-resolution + trust-store path below
exists for an adopter who has not yet pinned those fields (optional in
backend/schema_validator.py) and self-heals on first successful resolution.

CLI:
    python3 scripts/lib/trust_id_resolver.py migrate
    python3 scripts/lib/trust_id_resolver.py detect
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / ".autonomous-team" / "config.json"

# ---------------------------------------------------------------------------
# Three-state resolution result
# ---------------------------------------------------------------------------

RESOLVED = "resolved"
ABSENT = "absent"
UNKNOWN = "unknown"

_USER_QUERY = "query($l:String!){ user(login:$l){ id createdAt } }"


def resolve_login_to_id(login: str, *, timeout: int = 15, run: Optional[Callable] = None) -> dict:
    """Resolve *login* to its immutable GraphQL node ID.

    Returns ``{"state": RESOLVED|ABSENT|UNKNOWN, "id": str|None, "created_at": str|None}``.

    R1: parses stdout REGARDLESS of the subprocess exit code. Measured (D#1840
    panel, both the technical-architect and security-expert independently):
    ``gh api graphql`` exits 1 on a typed ``NOT_FOUND`` GraphQL error even
    though the underlying HTTP response was 200 with a fully parseable body.
    A bare ``returncode != 0`` check — the pattern external_intake_gate.py's
    ``_gh_graphql()`` uses at :429-430, correctly, for its own caller — would
    conflate ABSENT with UNKNOWN here, which is the one thing this resolver
    must never do (R3, enforced by callers, not here — this function only
    reports what it found).

    *run* is an injectable ``subprocess.run``-alike for tests: called with the
    same positional/keyword shape this function uses, must return an object
    exposing ``.returncode`` and ``.stdout``, or raise to simulate a
    subprocess-level failure (timeout, missing binary, etc).
    """
    args = ["gh", "api", "graphql", "-f", f"query={_USER_QUERY}", "-f", f"l={login}"]
    runner = run or subprocess.run
    try:
        result = runner(args, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 — any subprocess-level failure is UNKNOWN, never ABSENT
        return {"state": UNKNOWN, "id": None, "created_at": None}

    try:
        data = json.loads(result.stdout)
    except Exception:  # noqa: BLE001 — unparseable stdout (empty, truncated, non-JSON) is UNKNOWN
        return {"state": UNKNOWN, "id": None, "created_at": None}

    errors = data.get("errors") or []
    if any(isinstance(e, dict) and e.get("type") == "NOT_FOUND" for e in errors):
        return {"state": ABSENT, "id": None, "created_at": None}

    user = ((data.get("data") or {}).get("user")) or {}
    node_id = user.get("id")
    if node_id:
        return {"state": RESOLVED, "id": node_id, "created_at": user.get("createdAt")}

    # Parsed successfully, but neither a typed NOT_FOUND nor usable user data
    # — e.g. a permission/rate-limit error shape with a different errors[]
    # type. Fail closed to UNKNOWN rather than guessing.
    return {"state": UNKNOWN, "id": None, "created_at": None}


# ---------------------------------------------------------------------------
# Collaborator ID fetch — field-selection variant of external_intake_gate's
# _fetch_collaborators(), pulling node_id instead of login. Deliberately a
# SEPARATE function (not a modification of _fetch_collaborators) so every
# existing test that stubs collaborators_fetcher with login-shaped fixtures
# keeps working unchanged.
# ---------------------------------------------------------------------------


def fetch_collaborator_ids(repo_slug: str) -> set:
    """Return the set of node IDs for push/admin collaborators on *repo_slug*.

    Same endpoint external_intake_gate._fetch_collaborators() already calls;
    this only selects a different field (node_id instead of login) from a
    payload measured to carry both — zero net-new API round-trips relative
    to the existing collaborator fetch.
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
        ids = set()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            perms = entry.get("permissions") or {}
            if perms.get("push") or perms.get("admin"):
                node_id = entry.get("node_id")
                if node_id:
                    ids.add(node_id)
        return ids
    except Exception:  # noqa: BLE001 — fail closed: no extra trust from a broken fetch
        return set()


# ---------------------------------------------------------------------------
# ID collaborator cache — separate file from external_intake_gate's login
# cache (external_intake_allowlist_cache.json). A distinct path structurally
# rules out the login-cache-misread-as-IDs poisoning path (TA-3) rather than
# relying on a version discriminator inside a shared file; the schema key
# below is defence-in-depth on top of that, not the primary guard.
# ---------------------------------------------------------------------------

_ID_CACHE_SCHEMA = 1
_TRUST_STORE_SCHEMA = 1
CACHE_TTL_SECONDS = 3600


def _state_dir_path(filename: str) -> Path:
    """Mirrors external_intake_gate._default_cache_path()'s resolution order
    and its narrow `except ImportError` — see that function's docstring for
    why a broad `except Exception` here would be wrong (it would also
    swallow state_paths.UnsandboxedStatePathError, defeating D#1810's
    pytest guard).
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.state_paths import STATE_DIR, ensure_state_dir  # type: ignore

        ensure_state_dir()
        return Path(STATE_DIR) / filename
    except ImportError:
        return _REPO_ROOT / ".autonomous-team" / f".{filename}"


def default_id_cache_path() -> Path:
    return _state_dir_path("external_intake_id_cache.json")


def default_trust_store_path() -> Path:
    return _state_dir_path("external_intake_trust_store.json")


def read_id_cache(cache_path: Path) -> Optional[set]:
    try:
        data = json.loads(cache_path.read_text())
        if not isinstance(data, dict) or data.get("schema") != _ID_CACHE_SCHEMA:
            return None
        cached_at = data.get("cached_at", 0)
        if (time.time() - cached_at) < CACHE_TTL_SECONDS:
            return set(data.get("ids", []))
    except Exception:  # noqa: BLE001
        pass
    return None


def write_id_cache(cache_path: Path, ids: set) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"schema": _ID_CACHE_SCHEMA, "cached_at": time.time(), "ids": sorted(ids)})
        )
    except Exception:  # noqa: BLE001
        pass  # non-fatal — worst case we re-fetch next call


# ---------------------------------------------------------------------------
# Trust store — R4: written only from a fully successful resolution, never
# partially. A corrupt/unparseable store reads as UNKNOWN, not empty.
# Keyed by the LOGIN that was resolved (not by config field name) so it
# naturally covers boss, bot, and any maintainer_allowlist entry with one
# shape, and self-invalidates if an operator changes which login a config
# field points at (a deliberate config edit, not a transient failure, is
# exactly the case where re-resolving rather than trusting a stale entry is
# correct).
# ---------------------------------------------------------------------------


def _read_trust_store(path: Optional[Path] = None) -> Optional[dict]:
    """Returns the entries dict on a legitimate read (including "file does
    not exist yet" -> {}), or None if the file exists but is corrupt,
    unparseable, or carries an unrecognised schema version (R4 — this MUST
    be distinguished from an empty store by every caller).
    """
    p = path or default_trust_store_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("schema") != _TRUST_STORE_SCHEMA:
        return None
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None
    return entries


def _write_trust_store(entries: dict, path: Optional[Path] = None) -> None:
    p = path or default_trust_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": _TRUST_STORE_SCHEMA, "entries": entries}))


def get_stored_id(login: str, path: Optional[Path] = None) -> Optional[str]:
    """Last-known-good ID for *login*, or None if never resolved OR the store
    is unreadable. Both cases mean "no usable fallback" to a caller deciding
    whether to grant last-known-good trust — see resolve_allowlist_ids()'s
    bot-availability branch, which is the only caller that treats this as a
    positive trust grant. The distinction between "never resolved" and
    "store corrupt" is preserved at the _read_trust_store() layer (tested
    directly) for operators/logging, not lost — just not needed by trust
    decisions, which treat both as "nothing to fall back to".
    """
    entries = _read_trust_store(path)
    if entries is None:
        return None
    entry = entries.get(login)
    if not entry:
        return None
    return entry.get("id")


def record_resolved_id(login: str, node_id: str, path: Optional[Path] = None) -> None:
    """The ONLY place trust-store writes happen. Only called after a
    RESOLVED result — never from an ABSENT or UNKNOWN branch, and never
    partially (this always writes id+login+resolved_at together).
    """
    entries = _read_trust_store(path)
    if entries is None:
        entries = {}
    entries = dict(entries)
    entries[login] = {"id": node_id, "resolved_at": time.time()}
    _write_trust_store(entries, path)


# ---------------------------------------------------------------------------
# Detection (AC-10) — off every hot path. Resolves each configured trust
# literal and reports whether it still points at the account it was pinned
# for.
# ---------------------------------------------------------------------------


def detect_trust_drift(
    config: dict,
    *,
    resolver: Optional[Callable[[str], dict]] = None,
    trust_store_path: Optional[Path] = None,
) -> list:
    """For each configured trust literal (bot_account, boss_github_username,
    maintainer_allowlist entries), re-resolve its login and report a finding
    if anything looks wrong. Returns a list of dicts, empty when everything
    checks out. Never mutates config; never falls back to a login comparison
    (this is read-only reporting, not a trust decision).

    Finding "kind" values:
      "unresolvable"        login no longer resolves to any account (ABSENT)
      "id_mismatch"         resolves, but to a DIFFERENT id than the pinned
                             (config or last-recorded) one
      "suspicious_creation"  resolves to an id whose account creation date is
                             AFTER this entry was pinned/recorded — the
                             signature of "someone registered the freed
                             login", not just "we never checked before"
    """
    resolver = resolver or resolve_login_to_id
    findings = []

    literals = []
    bot_login = config.get("bot_account")
    if bot_login:
        literals.append(("bot_account", bot_login, config.get("bot_account_id")))
    boss_login = config.get("boss_github_username")
    if boss_login:
        literals.append(("boss_github_username", boss_login, config.get("boss_github_user_id")))
    for login in config.get("maintainer_allowlist") or []:
        if login not in (bot_login, boss_login):
            literals.append(("maintainer_allowlist", login, None))

    for field_name, login, pinned_id in literals:
        res = resolver(login)
        recorded = pinned_id or get_stored_id(login, path=trust_store_path)

        if res["state"] == ABSENT:
            findings.append({"field": field_name, "login": login, "kind": "unresolvable"})
            continue
        if res["state"] == UNKNOWN:
            # Could not tell — not a finding by itself (that would be
            # indistinguishable from an ordinary rate limit), detection just
            # skips this literal for this pass.
            continue

        # RESOLVED
        if recorded and res["id"] != recorded:
            findings.append(
                {"field": field_name, "login": login, "kind": "id_mismatch", "resolved_id": res["id"], "recorded_id": recorded}
            )
            continue

        # Only consult the trust store when this literal wasn't already
        # pinned in config — config doesn't carry a resolved_at timestamp,
        # so a config-pinned literal has nothing to compare created_at
        # against here, and reading the store at all would be pointless
        # I/O (and, for an adopter who has fully migrated to config-pinned
        # IDs, entirely avoidable).
        pinned_at = None
        if not pinned_id:
            recorded_entry = _read_trust_store(trust_store_path) or {}
            pinned_at = (recorded_entry.get(login) or {}).get("resolved_at")
        if res.get("created_at") and pinned_at:
            # created_at is an ISO8601 string; resolved_at is a unix epoch
            # float. Compare via a lightweight parse rather than pulling in
            # a dependency for this one comparison.
            try:
                import datetime

                created_ts = datetime.datetime.fromisoformat(
                    res["created_at"].replace("Z", "+00:00")
                ).timestamp()
                if created_ts > pinned_at:
                    findings.append(
                        {"field": field_name, "login": login, "kind": "suspicious_creation", "created_at": res["created_at"]}
                    )
            except Exception:  # noqa: BLE001 — best-effort; never raise out of detection
                pass

    return findings


# ---------------------------------------------------------------------------
# Migration (AC-16) — offline, human-run. Resolves bot_account / (and
# boss_github_username if present) to IDs and returns what to write into
# config. A login that cannot be resolved is a LOUD ABORT, never a silent
# drop: migration can afford to fail and be re-run; converting a trusted
# principal into an untrusted one silently is the one outcome worse than
# stopping.
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    pass


def migrate_config_ids(config: dict, *, resolver: Optional[Callable[[str], dict]] = None) -> dict:
    """Resolve bot_account and boss_github_username to node IDs.

    Returns {"bot_account_id": ..., "boss_github_user_id": ...} (only for the
    literals present in *config*). Raises MigrationError naming the
    unresolvable entry on ABSENT or UNKNOWN — never returns a partial result
    that silently omits an entry the caller could mistake for "not present".
    """
    resolver = resolver or resolve_login_to_id
    out = {}

    bot_login = config.get("bot_account")
    if bot_login:
        res = resolver(bot_login)
        if res["state"] != RESOLVED:
            raise MigrationError(
                f"could not resolve bot_account {bot_login!r} to an id (state={res['state']}); "
                "migration aborted — no config written. Re-run once the account is reachable."
            )
        out["bot_account_id"] = res["id"]
        record_resolved_id(bot_login, res["id"])

    boss_login = config.get("boss_github_username")
    if boss_login:
        res = resolver(boss_login)
        if res["state"] != RESOLVED:
            raise MigrationError(
                f"could not resolve boss_github_username {boss_login!r} to an id (state={res['state']}); "
                "migration aborted — no config written. Re-run once the account is reachable."
            )
        out["boss_github_user_id"] = res["id"]
        record_resolved_id(boss_login, res["id"])

    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage:\n"
            "  python3 scripts/lib/trust_id_resolver.py migrate\n"
            "  python3 scripts/lib/trust_id_resolver.py detect\n"
        )
        sys.exit(2)

    cmd = sys.argv[1]
    cfg = json.loads(_DEFAULT_CONFIG_PATH.read_text())

    if cmd == "migrate":
        try:
            resolved = migrate_config_ids(cfg)
        except MigrationError as exc:
            sys.stderr.write(f"migration aborted: {exc}\n")
            sys.exit(1)
        print(json.dumps(resolved, indent=2))

    elif cmd == "detect":
        findings = detect_trust_drift(cfg)
        print(json.dumps(findings, indent=2))
        sys.exit(1 if findings else 0)

    else:
        sys.stderr.write(f"Unknown command: {cmd}\n")
        sys.exit(2)
