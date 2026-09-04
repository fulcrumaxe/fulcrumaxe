"""scripts/lib/intake_baseline.py — content baseline store for the external-intake gate.

D#1672 (HG-6 real fix): binds `intake-approved` to the *content* that was
approved, not just the Discussion number. `external_intake_gate.py` calls into
this module to record what an external Discussion looked like at the moment a
human approved it, and to detect drift (an edit) on every later gate check.

Pure storage layer — no GitHub API calls, no subprocess. All I/O goes through
an injectable `path` argument so tests use `tmp_path` (never the real store).

Store shape (single JSON file, atomic tmp-file + os.replace()):

    {"version": 1, "baselines": {"fulcrumaxe/fulcrumaxe#1672": {
        "content_sha256": "...", "observed_last_edited_at": null,
        "observed_edit_count": 0, "observed_editor": null,
        "observed_at": "...", "invalidation_count": 0,
        "baseline_source": "first_observed_approval",
        "dismissed_content_sha256": null}}}

``dismissed_content_sha256`` (D#1672 round 2, SEC-1 fix): set the moment a
"drifted" verdict is actually dismissed (comment posted, counter bumped).
Lets a retried dismissal — the label removal mutation failed and the row is
still present on the next reconcile pass — recognise "I already dismissed
this exact content generation" and skip re-posting/re-bumping, while still
retrying the label removal itself every pass. See
``external_intake_gate.py::_reconcile_baseline`` for the call site.

A sibling sentinel file next to the store (``.<name>.initialized``) records
that the store has been written at least once (SEC-2 fix, D#1672 round 2) —
see ``read_baselines()`` for why this matters: without it, a *deleted* store
is indistinguishable from a store that was *never created*, and the former
must fail closed while the latter must not.

Key format is repo-scoped: "{owner}/{name}#{number}" — a bare Discussion
number is rejected (R9: the store is the thing that starts persisting
security state under a repo slug, so it must not be ambiguous about which
repo a row belongs to).

Invalidation predicate (R1, security-expert wins over TA): drift trips when
ANY of content hash / lastEditedAt / userContentEdits.totalCount has moved —
never AND, never hash-only. A matching hash after edit-then-revert does NOT
clear a tripped timestamp or counter (see check_baseline()).

Do NOT copy `_write_collaborator_cache()` in external_intake_gate.py:112 — that
helper does a bare non-atomic `write_text()`. This module always goes through
`_atomic_write()` (tmp file + os.replace(), re-read-merge immediately before
write) because pre-spawn-check.sh fires per spawn while team-lead-iteration.sh
loops all open Discussions, so concurrent read-modify-write is real, not
theoretical.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Repo-scoped key format: "{owner}/{name}#{number}". A bare integer/str
#: number is rejected — see _validate_key() and AC-8.
_KEY_RE = re.compile(r"^[^/\s#]+/[^/\s#]+#\d+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_key(key: object) -> str:
    """Return *key* if it is a valid "{owner}/{name}#{number}" string.

    Raises ValueError for anything else, including a bare int/str Discussion
    number — this is a deliberate hard-stop, not a soft coercion, because a
    bare-number key would silently share baselines across every repo this
    module is ever pointed at (R9).
    """
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ValueError(
            f"invalid baseline key {key!r} — must be '{{owner}}/{{name}}#{{number}}'"
        )
    return key


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------


def content_hash(title: str, body: str) -> str:
    """sha256 over raw UTF-8 ``title + "\\n\\n" + body`` — no normalization.

    Deliberately hashes the RAW title/body, never the output of
    sanitize_and_delimit_external(): hashing the sanitizer's output would make
    any content the sanitizer strips invisible to drift detection, and a
    future sanitizer change would silently redefine what "unchanged" means
    (TA, R1). This function does not import or call the sanitizer at all.
    """
    payload = f"{title or ''}\n\n{body or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Store path resolution
# ---------------------------------------------------------------------------


def _default_store_path() -> Path:
    """Store lives under the runtime state dir so it survives worktree churn.

    Falls back to a repo-local dotfile if state_paths is unavailable (e.g.
    this module imported standalone without the backend package on sys.path)
    — matches external_intake_gate.py's _default_cache_path() at lines 85-98.
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.state_paths import EXTERNAL_INTAKE_BASELINES, ensure_state_dir  # type: ignore

        ensure_state_dir()
        return Path(EXTERNAL_INTAKE_BASELINES)
    except ImportError:
        # Fallback for environments where state_paths is not on sys.path.
        # Deliberately narrow (not `except Exception`): a bare Exception
        # catch here would also swallow state_paths.UnsandboxedStatePathError
        # (D#1810's PYTEST_CURRENT_TEST guard, a RuntimeError subclass) and
        # silently relocate the baseline store to the repo-local fallback —
        # defeating the fail-closed property that guard exists to provide.
        # Matches the pattern already used in spec_external_docs.py.
        return _REPO_ROOT / ".autonomous-team" / "external-intake-baselines.json"


# ---------------------------------------------------------------------------
# Store I/O
# ---------------------------------------------------------------------------


def read_baselines(path: Optional[Path] = None) -> tuple[bool, dict]:
    """Read the baseline store. Returns (ok, data).

    ok=True includes the legitimate "store not created yet" case (a plain
    FileNotFoundError with no init marker on disk) — that is the steady-state
    first-ever-run condition, not a failure, and is returned as an empty
    store so the caller's lookup naturally resolves to "absent" for any key.

    SEC-2 fix (D#1672 round 2): a FileNotFoundError where the init marker
    (``.{name}.initialized``, written by ``_atomic_write`` the first time
    this store is ever successfully written) DOES exist is NOT the same
    condition — it means the store existed and is now gone, which is
    operationally indistinguishable from an attacker (or anything else with
    write access to the state dir) deleting it. Before this fix, deletion
    silently collapsed to the same "no entries yet" result as a genuine
    first run, making `rm` an unconditional auto-approve primitive for every
    currently-labelled external Discussion — worse than corruption, which
    already correctly failed closed. So: marker present + file missing ->
    ok=False ("unknown"), same as corruption.

    ok=False is also returned for everything else: unparseable JSON, a
    permission error, or a directory that cannot even be read to check
    whether the file is there (AC-6 — "missing-but-directory-unreadable" is
    NOT the same as "missing"). Callers MUST treat ok=False as "unknown" and
    fail closed — never coerce it into "no entries" (this is the same class
    of bug HG-7 Batch B already fixed once in external_intake_gate.py: an
    empty result from a failed read/fetch must never look identical to a
    genuinely empty successful one).
    """
    p = path or _default_store_path()
    try:
        raw = p.read_text()
    except FileNotFoundError:
        if _marker_path(p).exists():
            return False, {}
        return True, {"version": 1, "baselines": {}}
    except OSError:
        return False, {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False, {}
    if not isinstance(data, dict) or not isinstance(data.get("baselines"), dict):
        return False, {}
    return True, data


def _marker_path(store_path: Path) -> Path:
    """Sentinel file living next to *store_path*: its existence means the
    store has been written at least once (SEC-2, D#1672 round 2). See
    read_baselines() for why this distinction matters.
    """
    return store_path.with_name(f".{store_path.name}.initialized")


def _touch_marker(store_path: Path) -> None:
    """Write the marker. Never raises — a caller mid-write must not fail just
    because the marker couldn't be touched. Unlike other best-effort writers
    in this module, a *failed* marker write is not silently swallowed with no
    other signal (SEC-5 fix, D#1672 round 3): a marker write that fails here
    permanently disables the fail-closed store-deletion protection (see
    read_baselines()) with nothing recorded anywhere, so a failure is routed
    through the audit trail before being suppressed.
    """
    try:
        _marker_path(store_path).write_text(_now_iso())
    except Exception as exc:  # noqa: BLE001
        _audit_marker_write_failure(store_path, exc)


def _audit_marker_write_failure(store_path: Path, exc: Exception) -> None:
    """Best-effort audit emit for a failed marker write. Never raises — this
    is a secondary signal, not a control, so its own failure must not
    propagate. Mirrors external_intake_gate.py::_audit_baseline_transition's
    lazy-import pattern (this module stays import-clean when backend isn't on
    sys.path, e.g. imported standalone).
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from backend.audit_trail import get_audit_trail  # type: ignore  # noqa: PLC0415

        get_audit_trail().emit(
            "intake_baseline",
            "marker_write_failed",
            str(_marker_path(store_path)),
            None,
            str(exc),
            "system",
        )
    except Exception:  # noqa: BLE001
        pass


def _atomic_write(path: Path, data: dict) -> None:
    """tmp-file + os.replace() — never a bare write_text() (AC-10).

    SEC-5 fix (D#1672 round 3): the marker is touched BEFORE os.replace()
    makes the store visible, not after. Anything that interrupts between the
    two steps (crash, SIGKILL, container stop) now leaves marker-present /
    store-absent instead of the reverse. That inverted state resolves to
    ok=False -> "unknown" -> blocked in read_baselines() — the safe
    direction — and self-heals on the very next read-modify-write cycle,
    since _read_modify_write() already rebuilds from {} whenever ok=False.
    The previous order (replace, then touch) left a store-present /
    marker-absent window instead, which is operationally indistinguishable
    from a deleted store and made every deletion fail OPEN — the exact SEC-2
    bypass this marker exists to prevent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    _touch_marker(path)
    os.replace(tmp, path)


def _read_modify_write(path: Path, mutate) -> dict:
    """Re-read the store immediately before writing, apply *mutate* in place,
    then atomic-write. This is the concurrency mitigation (AC-10): pre-spawn-
    check.sh fires per spawn while team-lead-iteration.sh loops every open
    Discussion, so two writers can race. Re-reading right before the write
    (rather than relying on a value read earlier in the caller) means a
    concurrent writer's change already on disk survives into this write too.
    """
    ok, data = read_baselines(path)
    if not ok:
        data = {"version": 1, "baselines": {}}
    mutate(data)
    _atomic_write(path, data)
    return data


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def record_baseline(
    key: str,
    *,
    content_sha256: str,
    last_edited_at: Optional[str],
    edit_count: int,
    editor: Optional[str],
    path: Optional[Path] = None,
    source: str = "first_observed_approval",
) -> None:
    """Upsert the baseline row for *key*. Keyed upsert, never append (AC-11)."""
    key = _validate_key(key)
    p = path or _default_store_path()

    def _mutate(data: dict) -> None:
        existing = data["baselines"].get(key) or {}
        data["baselines"][key] = {
            "content_sha256": content_sha256,
            "observed_last_edited_at": last_edited_at,
            "observed_edit_count": edit_count,
            "observed_editor": editor,
            "observed_at": _now_iso(),
            "invalidation_count": existing.get("invalidation_count", 0),
            "baseline_source": source,
        }

    _read_modify_write(p, _mutate)


def drop_baseline(key: str, path: Optional[Path] = None) -> bool:
    """Remove the baseline row for *key* if present. Returns True if a row was
    actually removed, False if there was nothing to drop (or the store could
    not be read — fails closed by doing nothing, never raises).
    """
    key = _validate_key(key)
    p = path or _default_store_path()
    ok, data = read_baselines(p)
    if not ok or key not in data.get("baselines", {}):
        return False

    removed = {"did": False}

    def _mutate(data: dict) -> None:
        if key in data.get("baselines", {}):
            del data["baselines"][key]
            removed["did"] = True

    _read_modify_write(p, _mutate)
    return removed["did"]


def bump_invalidation(key: str, path: Optional[Path] = None) -> int:
    """Increment invalidation_count on the existing row for *key* and return
    the new value. No-op (returns 0) if the row does not exist — callers only
    invoke this on a row they already know is present (a drifted verdict
    implies an entry exists; see check_baseline()).
    """
    key = _validate_key(key)
    p = path or _default_store_path()
    result = {"count": 0}

    def _mutate(data: dict) -> None:
        entry = data["baselines"].get(key)
        if entry is None:
            return
        entry["invalidation_count"] = entry.get("invalidation_count", 0) + 1
        result["count"] = entry["invalidation_count"]

    _read_modify_write(p, _mutate)
    return result["count"]


def mark_dismissed(key: str, content_sha256: str, path: Optional[Path] = None) -> None:
    """Record that a dismissal (comment posted, invalidation_count bumped) has
    already happened for *content_sha256* on this row (SEC-1 fix, D#1672
    round 2). The row is deliberately NOT dropped as part of dismissal
    anymore — see external_intake_gate.py::_reconcile_baseline for why — so a
    label-removal mutation that fails transiently gets retried on every
    subsequent reconcile pass while the Discussion stays in the "drifted"
    verdict. Without this marker, each retry would re-post the dismissal
    comment and re-bump the counter for content that hasn't changed again.
    No-op if the row does not exist.
    """
    key = _validate_key(key)
    p = path or _default_store_path()

    def _mutate(data: dict) -> None:
        entry = data["baselines"].get(key)
        if entry is None:
            return
        entry["dismissed_content_sha256"] = content_sha256

    _read_modify_write(p, _mutate)


def get_entry(key: str, path: Optional[Path] = None) -> Optional[dict]:
    """Return the raw stored row for *key*, or None if absent or unreadable."""
    key = _validate_key(key)
    ok, data = read_baselines(path)
    if not ok:
        return None
    return data.get("baselines", {}).get(key)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def check_baseline(key: str, current: dict, path: Optional[Path] = None) -> str:
    """Compare *current* observation against the stored baseline for *key*.

    *current* = {"content_sha256": str, "last_edited_at": Optional[str],
                 "edit_count": int}

    Returns one of:
      "unknown" — the store itself could not be read (AC-6). Caller must fail
                  closed; this is NOT the same as "absent".
      "absent"  — store read fine, no row for this key (steady-state first
                  observation — AC-7).
      "match"   — content hash, lastEditedAt, and edit count all match the
                  baseline.
      "drifted" — ANY of the three signals moved (R1: OR, not AND). A matching
                  hash never clears a tripped timestamp or counter — this is
                  the edit-then-revert case (AC-3): the interleaving already
                  happened, a revert doesn't undo it.
    """
    key = _validate_key(key)
    ok, data = read_baselines(path)
    if not ok:
        return "unknown"

    entry = data.get("baselines", {}).get(key)
    if entry is None:
        return "absent"

    hash_diff = current.get("content_sha256") != entry.get("content_sha256")

    cur_ts = current.get("last_edited_at")
    base_ts = entry.get("observed_last_edited_at")
    ts_advance = cur_ts is not None and (base_ts is None or cur_ts > base_ts)

    cur_count = current.get("edit_count") or 0
    base_count = entry.get("observed_edit_count") or 0
    count_advance = cur_count > base_count

    if hash_diff or ts_advance or count_advance:
        return "drifted"
    return "match"
