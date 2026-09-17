"""scripts/lib/route_discussion_wiring.py — side-effects layer for the Discussion router.

Handles:
  - stdin/stdout JSON I/O
  - control-plane gate check (gates.cost_aware_router)
  - body sanitization before embedding into executor prompts
  - /route:<directive> override parsing (requires the comment author's
    immutable GitHub node ID to resolve to boss_github_user_id — see
    _parse_override's docstring and D#1990)
  - audit log write to .autonomous-team/route-decisions.jsonl

The pure routing logic lives in route_discussion.py — this module is the
shell around it.

CLI usage:
  echo '{"discussion":836,"body":"...","labels":["Feature"]}' | python3 route_discussion_wiring.py
  # stdout: routing decision JSON (or null if gate is off)
  # stderr: audit log write confirmation
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_AUDIT_LOG = _REPO_ROOT / ".autonomous-team" / "route-decisions.jsonl"
_DEFAULT_CONFIG_PATH = _REPO_ROOT / ".autonomous-team" / "config.json"
_BODY_MAX_LEN = 4000

# D#2608: deleting a matched token or comment splices the text on either
# side of the match back together. Two differently-shaped bugs are the SAME
# root cause, and a fix that only closes one of them is not the fix:
#   - MANUFACTURE: "SPAWN_<!--x-->REQUEST" doesn't match the SPAWN_REQUEST
#     pattern below (a comment is in the way) — but deleting "<!--x-->"
#     makes "SPAWN_" and "REQUEST ..." adjacent, spelling a token that was
#     never in the input and is never re-scanned for.
#   - SPLICE ACROSS A DIFFERENT PATTERN: "SPAWN_TERMINATE_REQUEST pad\n
#     REQUEST ..." doesn't match SPAWN_REQUEST either (TERMINATE_ is in the
#     way) — but deleting the TERMINATE_REQUEST match, a DIFFERENT pattern
#     with no comment involved at all, splices "SPAWN_" and "REQUEST ..."
#     together the same way. Pattern N's deletion can complete pattern M's
#     token for any M that already ran. This is NOT about whether an
#     earlier pass could fragment a pattern's own literal text (that
#     direction is fine); it is the other direction, and every pattern
#     below is exposed to it — not just the comment one.
# Every pattern therefore replaces its match with a visible marker instead
# of deleting it, so two leftover fragments can never rejoin — through the
# marker or through each other — into a new match. This is the same
# one-marker-for-every-pattern shape the hosted product's TypeScript port
# of this sanitizer uses (packages/trust/src/sanitize.ts's
# stripControlTokens()), for the same reason.
#
# The marker must not itself contain any of the four sanitized token
# shapes, and must not contain "<<" or ">>" (external_intake_gate.
# sanitize_and_delimit_external() wraps this output in <<UNTRUSTED...>>
# fences). It carries no authenticity: an author can type the literal text
# "[removed]" themselves, and nothing here or downstream parses, counts, or
# reconstructs anything from an occurrence of this marker — it exists only
# so a human glancing at the text can see something was removed. Do not
# build decision logic against it.
_CONTROL_TOKEN_MARKER = "[removed]"

_SANITIZE_PATTERNS = [
    # Each pattern below captures its own optional trailing newline in a
    # group and reinserts it after the marker (replacement "...\1"), rather
    # than consuming it silently. Without this, replacing "SPAWN_REQUEST:
    # ...\n" with a bare marker deletes the newline along with the match,
    # so whatever starts the NEXT line is no longer preceded by a newline —
    # which silently defeats the STATUS: pattern's "^" line-start anchor
    # below for a genuine STATUS: token one line down (caught by
    # backend/tests/test_pr_comment_trust.py::test_untrusted_body_is_delimited_and_sanitized
    # during D#2608 review round 2). The marker itself never carries any of
    # the stripped content — only this one content-free newline does.
    (re.compile(r"SPAWN_REQUEST[^\n]*(\n?)", re.MULTILINE), _CONTROL_TOKEN_MARKER + r"\1"),
    (re.compile(r"TERMINATE_REQUEST[^\n]*(\n?)", re.MULTILINE), _CONTROL_TOKEN_MARKER + r"\1"),
    # Anchored to line-start ("^", with MULTILINE): a genuine control token
    # is a line by itself at the start of a line. Without this anchor,
    # "STATUS:" embedded inside a well-formed HTML comment on the same line
    # — the canonical "<!-- STATUS:SPEC_READY ... -->" usage throughout this
    # codebase — matches THROUGH that comment's own closing "-->", deleting
    # it and leaving a dangling "<!--" that then swallows everything up to
    # the NEXT unrelated comment's closer once the pattern below runs. Same
    # collateral-damage shape a greedy comment match caused, via a
    # different pattern. Verified: without this anchor, a body with a
    # leading STATUS comment and one unrelated comment later loses
    # everything between them; with it, both comments are stripped
    # independently and the real text between them survives.
    (re.compile(r"^STATUS:[A-Z_]+[^\n]*(\n?)", re.MULTILINE), _CONTROL_TOKEN_MARKER + r"\1"),
    # Non-greedy with an end-of-input fallback: matches to the FIRST "-->"
    # it finds, or to end-of-input when there is none. Deliberately NOT
    # greedy-to-the-last-"-->" (an earlier version of this fix used greedy
    # matching) — greedy also defeats the nested-comment repro below, but
    # it additionally merges any two SEPARATE, well-formed comments
    # anywhere in the body into one match, deleting real prose between
    # them (e.g. a body with a leading "<!-- STATUS:... -->" and an
    # unrelated comment later would lose everything in between). The
    # marker alone already defeats the nested/interleaved repro:
    # sanitize_body("<!-<!--a-->- AGENT_OUTPUT --<!--b-->>") produces
    # "<!-[removed]- AGENT_OUTPUT --[removed]>" — the words are still
    # there, but there is no "<!--" and no "-->" left in the output, so
    # nothing downstream can parse it as a comment or an envelope. Same
    # shape as the hosted TypeScript port's HTML_COMMENT_PATTERN.
    (re.compile(r"<!--[\s\S]*?(?:-->|\Z)"), _CONTROL_TOKEN_MARKER),
]


def _strip_format_chars(text: str) -> str:
    """Drop Unicode category "Cf" (format) characters before any pattern
    above runs — zero-width joiners, bidi controls, BOM, soft hyphen, etc.,
    used to split a denylisted token so a naive exact-text scan misses it
    (e.g. "SPAWN​_REQUEST", "STATUS­:SPEC_READY"). Must run BEFORE
    the token patterns: this strip itself deletes characters and therefore
    splices text the same way the patterns above used to — it is only safe
    here because nothing has looked for a token yet, so there is nothing to
    complete. Ported from the hosted product's TypeScript sanitizer
    (packages/trust/src/sanitize.ts); NFKC normalization and case-
    insensitive token matching are a deliberate follow-up, not this round.
    """
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


# ---------------------------------------------------------------------------
# Control-plane gate check
# ---------------------------------------------------------------------------


def _gate_enabled() -> bool:
    """Return True if gates.cost_aware_router is enabled (default: True)."""
    try:
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "control_plane.py"),
             "get", "gates.cost_aware_router"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            val = result.stdout.strip().strip('"').lower()
            return val not in ("false", "0", "no")
    except Exception:
        pass
    # Default: fail-closed — if we can't read the gate, don't route. Safer than
    # silently activating the router on a misconfigured control-plane subprocess.
    return False


def _load_config(config_path: Optional[Path] = None) -> dict:
    """Read .autonomous-team/config.json the same way external_intake_gate.py
    (D#1840) does — fail closed to an empty dict on any read/parse error, so
    a missing or malformed config never grants override capability."""
    path = config_path or _DEFAULT_CONFIG_PATH
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — fail closed: no config, no override capability
        return {}


def _log_loud(message: str) -> None:
    sys.stderr.write(f"[route_discussion_wiring] {message}\n")


# ---------------------------------------------------------------------------
# Body sanitization
# ---------------------------------------------------------------------------


def sanitize_body(body: str) -> str:
    """Strip control-plane tokens from *body* and cap at _BODY_MAX_LEN chars.

    Never modify the discussion body in-place — operates on a copy.
    Called by the wiring layer before embedding body into executor prompt.

    Takes exactly ONE author's text per call — never concatenate several
    authors' text into a single call before this function runs. The
    HTML-comment pattern's end-of-input fallback (see _SANITIZE_PATTERNS
    above) has no way to know where one author's text ends and the next
    begins, so an unterminated "<!--" from one author would consume every
    subsequent author's text in the same call. All current callers already
    satisfy this: pr_comment_trust.py calls this (via
    sanitize_and_delimit_external) once per comment, and the
    external_intake_gate CLI path passes one body per invocation.

    The length cap is applied to the INPUT before the regex loop runs (as
    well as to the output afterward, unchanged from before) — D#2608: capping
    only the output let an attacker place content past _BODY_MAX_LEN in the
    raw body, then have an earlier pattern's deletion shrink the string enough
    to pull that content into the kept window. Capping first means nothing
    past _BODY_MAX_LEN in the input can ever reach the regex loop, let alone
    the output.
    """
    sanitized = body[:_BODY_MAX_LEN]
    sanitized = _strip_format_chars(sanitized)
    for pattern, replacement in _SANITIZE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized[:_BODY_MAX_LEN]


# ---------------------------------------------------------------------------
# Override parsing
# ---------------------------------------------------------------------------


def _parse_override(
    comments: list[dict],
    boss_id: Optional[str],
    *,
    resolver: Optional[Callable[[str], dict]] = None,
) -> Optional[dict]:
    """Scan Discussion comments for /route:<directive> override.

    Valid signer (D#1990, sibling of D#1840/CWE-290): the comment author's
    *immutable GitHub node ID* — resolved via trust_id_resolver, never the
    mutable login — must equal the configured boss_github_user_id. The
    '[team-lead-signed]' prefix bypass was removed for the same reason
    (D#1588 HG-4): a bare login comparison, or any body-text token, lets an
    attacker forge authority. Only a resolved node ID is trusted.

    Three states, fail-closed with NO degradation (this is a higher-privilege
    action than provenance classification, so — unlike the bot-account
    handling in external_intake_gate.resolve_allowlist_ids — UNKNOWN here
    never falls back to a last-known-good stored ID):
      - RESOLVED and equal to boss_id  -> override honoured.
      - RESOLVED and different         -> refused, silently (not this signer).
      - ABSENT                         -> refused (login no longer resolves
                                           to any account).
      - UNKNOWN                        -> refused, and logged loudly. Never
                                           falls back to comparing the login
                                           string — that would hand the
                                           vulnerability back on an
                                           attacker-inducible path (make the
                                           resolver call fail/time out).

    boss_id is resolved once by the caller from config
    (boss_github_user_id) and threaded through — this function never
    resolves the boss's own identity, only the commenter's.

    Returns dict with {route, override_signer} (override_signer is the
    resolved node ID, never a login) or None.
    """
    if not boss_id:
        return None

    # Local import — see the identical comment on the route_discussion import
    # in route_with_wiring(): keeps this module importable standalone and
    # matches the lazy-import convention already used here.
    from trust_id_resolver import RESOLVED, UNKNOWN, resolve_login_to_id  # type: ignore[import]

    resolve_fn = resolver or resolve_login_to_id

    for comment in comments:
        body = comment.get("body", "")
        match = re.search(r"/route:\s*(\S+)", body)
        if not match:
            continue

        author = comment.get("author", {})
        login = author.get("login", "") if isinstance(author, dict) else (author or "")
        if not login:
            continue

        res = resolve_fn(login)
        state = res.get("state")

        if state == UNKNOWN:
            _log_loud(
                f"/route: override signer identity unknown for {login!r} — "
                "refusing the override (never falls back to login comparison)"
            )
            continue
        if state != RESOLVED:
            continue  # ABSENT — login no longer resolves to any account

        author_id = res.get("id")
        if author_id != boss_id:
            continue

        return {
            "route": match.group(1).strip(),
            "override_signer": author_id,
        }
    return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _write_audit(record: dict) -> None:
    """Append a routing decision to the audit log.

    Fields written: discussion, route, reason, recommended_model (from
    model_tier_hint), actual_model (the model that will be used for the
    spawned agent), model_tier_hint (kept for back-compat), labels_hash,
    decided_at, shadow (True when gate is off), override_signer (optional).

    Body text is NEVER written to the audit log.
    """
    safe = {
        "discussion": record.get("discussion"),
        "route": record.get("route"),
        "reason": record.get("reason"),
        "recommended_model": record.get("model_tier_hint"),
        "actual_model": record.get("actual_model"),
        "model_tier_hint": record.get("model_tier_hint"),
        "labels_hash": record.get("labels_hash"),
        "decided_at": record.get("decided_at"),
    }
    if record.get("shadow"):
        safe["shadow"] = True
    if "override_signer" in record:
        safe["override_signer"] = record["override_signer"]

    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(safe) + "\n")
    except OSError:
        pass  # Non-fatal — routing continues without audit write


# ---------------------------------------------------------------------------
# Main wiring function
# ---------------------------------------------------------------------------


def route_with_wiring(
    discussion: int,
    body: str,
    labels: list[str],
    comments: Optional[list[dict]] = None,
    config: Optional[dict] = None,
    actual_model: Optional[str] = None,
    resolver: Optional[Callable[[str], dict]] = None,
) -> Optional[dict]:
    """Run the router with side effects (gate check, override, audit log).

    Returns routing decision dict when the gate is enabled, or None when
    the gate is disabled (shadow mode).

    Shadow logging: the audit row is ALWAYS written regardless of gate state,
    so route-decisions.jsonl captures the model decision for observability
    even when cost_aware_router is off.  The gate only controls whether the
    routing decision is returned to the caller (i.e. whether it affects
    spawning behavior).

    Parameters
    ----------
    discussion:   Discussion number.
    body:         Raw Discussion body (used for routing logic only — never
                  logged to audit).
    labels:       Discussion label list.
    comments:     Optional list of Discussion comments for override parsing.
    config:       Optional pre-loaded control-plane config dict (mainly for
                  tests). Defaults to reading .autonomous-team/config.json
                  (D#1840's boss_github_user_id field — this module defines
                  no config field of its own). Missing/unreadable -> {} ->
                  no override capability (fail closed).
    actual_model: The model that will actually be used for the spawned agent
                  (e.g. from the role's .claude/agents/<role>.md frontmatter).
                  Logged in the audit row alongside recommended_model so the
                  two can be compared downstream.
    resolver:     Optional injectable replacement for
                  trust_id_resolver.resolve_login_to_id (tests only).

    The returned dict is safe to embed in spawn prompts — it contains no
    body excerpts.  Call sanitize_body() separately when building the prompt.
    """
    gate_on = _gate_enabled()

    # Always compute the routing decision for shadow logging — even when the
    # gate is off, we want the audit row so we can observe what WOULD have
    # been routed.
    # Import here to keep the pure function isolated from the wiring module.
    from route_discussion import route  # type: ignore[import]

    decision = route(discussion=discussion, body=body, labels=labels)

    # Check for manual override in Discussion comments (only meaningful when
    # gate is on, but apply to audit record regardless for observability).
    # boss_id is the immutable node ID from config (D#1840) — _parse_override
    # never sees or compares a login.
    if comments:
        cfg = config if config is not None else _load_config()
        boss_id = cfg.get("boss_github_user_id")
        override = _parse_override(comments, boss_id, resolver=resolver)
        if override:
            decision = dict(decision)
            decision["route"] = override["route"]
            decision["reason"] = "manual_override"
            decision["override_signer"] = override["override_signer"]

    # Build audit record.  Shadow flag marks gate-off rows clearly.
    audit_record = dict(decision, discussion=discussion)
    audit_record["actual_model"] = actual_model
    if not gate_on:
        audit_record["shadow"] = True
    _write_audit(audit_record)

    # Fail-closed on the behavior path: return None when gate is off so
    # the caller never applies routing logic unless explicitly enabled.
    if not gate_on:
        return None

    return decision


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    payload = json.load(sys.stdin)
    result = route_with_wiring(
        discussion=payload["discussion"],
        body=payload["body"],
        labels=payload.get("labels", []),
        comments=payload.get("comments"),
    )
    if result is None:
        print("null")
    else:
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
