"""
backend/envelope_check.py — envelope fabrication detector.

Detector, not gate. SubagentStop fires after the Agent() result has already
reached whoever spawned the agent, so this can only leave a finding for a
later reader to notice — it never refuses a route, never changes a verdict,
and never blocks anything. Nothing in this module raises out of `main()`;
every path that can fail resolves to a printed JSON object and exit 0.

## Two checks

1. `check_impossible_sources` — flags a run whose own transcript shows zero
   `tool_use` blocks but whose envelope carries a non-empty `sources`
   array. This is impossible-*looking*, not strictly impossible: the tri-
   state below rescues the case where the transcript could not be read at
   all, but a transcript that IS readable and merely truncated still counts
   zero `tool_use` blocks for an honest run too, so an honestly-truncated
   run shares this exact shape with a fabricated one. The false-positive
   tail is exactly that truncated-but-parseable population, not zero.

   That asymmetry has a consequence worth stating plainly: an agent trying
   to evade this check does better by deleting its own transcript (reads as
   unknown — `tool_uses is None` — and escapes untouched) than by doing
   nothing (reads as a real zero and gets caught). A single row here can
   never show that evasion; the signal it would leave is a population-level
   rise in how often `tool_uses` resolves to unknown, which is a job for a
   rate computed over many rows, not this per-envelope check.

2. `resolve_claimed_artifact` — one GraphQL call to confirm a claimed
   Discussion-comment permalink actually corresponds to a live comment.

## Tri-state tool_uses

Matches `scripts/lib/subagent_payload.py:count_tool_uses`: a non-negative
int when the run's own transcript was readable (0 included — a genuinely
zero-tool run), or `None`/"" when it could not be read at all. `None` must
never be treated as `0`. `check_impossible_sources` returns `finding: None,
reason: "tool_uses_unknown"` for that case — never a finding.

## Fail-closed on ambiguity, reporting direction only

`resolve_claimed_artifact` returns one of three things: `None` (resolved —
the artifact exists), `"claimed_artifact_unresolved"` (checked, and it does
not), or `"unverified"` (could not be checked at all — network error,
unrecognised URL shape, or more comments exist than one page can confirm
absence over). `unverified` never silently falls through to `None`.

## CLI

    python3 -m backend.envelope_check \\
        --tool-uses <int|""> --sources-count <int> \\
        [--claimed-artifact <url>] --json \\
        [--record --role <r> --discussion <n> --pr <n>]

`--tool-uses ""` (or omitted) means unknown. `--json` is accepted for
interface clarity but this always prints JSON. `--record` is what turns a
finding into a written artifact (one `audit.jsonl` row, one team-log line)
— it is the flag `scripts/hooks/post-agent.d/envelope-check.sh` passes when
wiring this into the live post-agent-hook chain. Without it, `main()` is a
pure evaluator with no side effects, safe to run repeatedly against the
live GitHub API.

At most one network call happens per invocation, and only when
`--claimed-artifact` is given — `check_impossible_sources` never touches
the network. That call carries a credential ONLY when talking to the
default `https://api.github.com` (never to an overridden host — see
`_graphql_call`) and never follows a redirect (a redirect response is
treated as a transport failure, not honoured).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Allow running as a script (`python3 backend/envelope_check.py ...`, which
# is how the hook invokes it) as well as `python3 -m backend.envelope_check`.
# `_state_dir()` below does `from backend import state_paths`, so the
# package has to be importable even when this file is executed directly —
# same fix as backend/audit_trail.py's own module docstring explains.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Matches a GitHub Discussion-comment permalink, e.g.
#   https://github.com/OWNER/REPO/discussions/123#discussioncomment-456
# A claimed-and-never-posted Discussion comment is the one artifact shape
# this module currently resolves; a PR-comment permalink would be the
# analogous `#issuecomment-<id>` shape, out of scope until an envelope
# actually claims one.
DISCUSSION_COMMENT_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)"
    r"/discussions/(?P<number>\d+)#discussioncomment-(?P<comment_id>\d+)"
)

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 5.0


def check_impossible_sources(tool_uses: int | None, sources_count: int) -> dict:
    """Structural-impossibility check 1.

    Returns ``{"finding": None, "reason": "tool_uses_unknown"}`` when
    ``tool_uses`` is ``None`` (unread/unreadable transcript — unknown is
    never zero), ``{"finding": "impossible_sources_without_tool_calls",
    "reason": ...}`` when the run made zero tool calls but the envelope
    carries a non-empty ``sources`` array, and ``{"finding": None, "reason":
    None}`` otherwise (a legitimate run, or a genuinely empty ``sources``
    array with zero tool calls — refusing with `sources: []` is the correct
    refusal path and is not itself suspicious).
    """
    if tool_uses is None:
        return {"finding": None, "reason": "tool_uses_unknown"}
    if tool_uses == 0 and sources_count and sources_count > 0:
        return {
            "finding": "impossible_sources_without_tool_calls",
            "reason": f"tool_uses=0 with sources_count={sources_count}",
        }
    return {"finding": None, "reason": None}


def _github_token() -> str:
    """GH_TOKEN / GITHUB_TOKEN win outright when set. Otherwise fall back to
    `gh auth token` — the GraphQL API 403s an unauthenticated request, and
    this project's own `gh` CLI is already authenticated in every
    environment that runs this (spawn logs confirm it resolves a working
    credential even when neither env var is exported into a given shell)."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        import subprocess  # noqa: PLC0415

        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5, check=False
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect rather than follow it. `redirect_request`
    returning None makes the underlying opener raise `HTTPError` for the
    3xx instead of silently re-issuing the request at `newurl` — see
    cpython's `HTTPRedirectHandler.http_error_302`. `resolve_claimed_artifact`
    treats that HTTPError like any other transport failure (`unverified`).

    Why this matters here specifically: `api_base` is env-overridable
    (`ENVELOPE_CHECK_GITHUB_API_BASE`, a test-only seam — see
    `resolve_claimed_artifact`), and this request carries a live GitHub
    credential (see `_graphql_call` below). `urllib` follows redirects by
    default and re-sends the `Authorization` header to wherever the 3xx
    points, so an overridden host that happens to redirect — no attacker
    required — would otherwise leak that credential off-host. Refusing the
    redirect outright is simpler and strictly safer than trying to strip
    the header conditionally on cross-host redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, PLR0913
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _graphql_call(query: str, variables: dict, api_base: str, timeout: float) -> dict:
    """One HTTP POST to ``<api_base>/graphql``. Any transport failure raises
    — the caller decides how to fold that into ``unverified``.

    The `Authorization` header is attached ONLY when `api_base` is exactly
    the real default (`https://api.github.com`) — never when it has been
    overridden. `api_base` is an env-controlled, agent-adjacent input (this
    function's only caller resolves it from `ENVELOPE_CHECK_GITHUB_API_BASE`,
    a seam meant for tests pointing at an unroutable host, not for talking
    to a different real API with this credential), so a live token must
    never ride along to a host this module did not choose."""
    headers = {"Content-Type": "application/json"}
    if api_base == DEFAULT_API_BASE:
        token = _github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(f"{api_base}/graphql", data=body, headers=headers, method="POST")
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


_DISCUSSION_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      comments(first: 100) {
        nodes { databaseId }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""


def resolve_claimed_artifact(url: str, api_base: str | None = None, timeout: float | None = None) -> dict:
    """Structural-impossibility check 2: does a claimed Discussion-comment
    artifact actually exist? One GraphQL call, at most.

    Returns ``{"finding": None, ...}`` when the comment id is live,
    ``{"finding": "claimed_artifact_unresolved", ...}`` when the discussion
    was reachable and the id is absent from it, or ``{"finding":
    "unverified", ...}`` when this could not be determined at all —
    unrecognised URL shape, unreachable API, an error response, a missing
    discussion, or (since this caps at one page / 100 comments) a
    discussion with more comments than the single call inspected. The last
    case fails toward "could not confirm absence" rather than a false
    accusation.
    """
    api_base = api_base or os.environ.get("ENVELOPE_CHECK_GITHUB_API_BASE", DEFAULT_API_BASE)
    if timeout is None:
        timeout = float(os.environ.get("ENVELOPE_CHECK_HTTP_TIMEOUT", DEFAULT_TIMEOUT))

    m = DISCUSSION_COMMENT_RE.search(url or "")
    if not m:
        return {"finding": "unverified", "reason": "unrecognized_artifact_url_shape"}

    owner, repo = m.group("owner"), m.group("repo")
    number, comment_id = int(m.group("number")), int(m.group("comment_id"))

    try:
        data = _graphql_call(
            _DISCUSSION_COMMENTS_QUERY,
            {"owner": owner, "name": repo, "number": number},
            api_base,
            timeout,
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure is "could not check"
        return {"finding": "unverified", "reason": f"github_api_unreachable: {exc.__class__.__name__}: {exc}"}

    if not isinstance(data, dict) or data.get("errors"):
        return {"finding": "unverified", "reason": f"github_api_error_response: {data.get('errors') if isinstance(data, dict) else data!r}"}

    discussion = ((data.get("data") or {}).get("repository") or {}).get("discussion")
    if not discussion:
        return {"finding": "unverified", "reason": "discussion_not_found_or_unreadable"}

    comments = discussion.get("comments") or {}
    nodes = comments.get("nodes") or []
    ids = {n.get("databaseId") for n in nodes if isinstance(n, dict)}

    if comment_id in ids:
        return {"finding": None, "reason": None}

    if (comments.get("pageInfo") or {}).get("hasNextPage"):
        # More comments exist than the one-call cap inspected — cannot
        # confirm absence, so this is not a resolved "unresolved".
        return {
            "finding": "unverified",
            "reason": f"comment id {comment_id} not among first {len(ids)} comments, and more exist (one-call cap)",
        }

    return {
        "finding": "claimed_artifact_unresolved",
        "reason": f"comment id {comment_id} not among {len(ids)} live comments",
    }


def extract_sources_count(envelope: dict) -> int:
    """``len(envelope["sources"])``, 0 for anything else. A shared helper so
    every caller (the CLI's own argv path and
    `scripts/hooks/post-agent.d/envelope-check.sh`) computes this from the
    parsed envelope the identical way."""
    if not isinstance(envelope, dict):
        return 0
    sources = envelope.get("sources")
    return len(sources) if isinstance(sources, list) else 0


def extract_claimed_artifact(envelope: dict) -> str | None:
    """Scan a PARSED envelope's own field values — never raw surrounding
    prose — for a Discussion-comment permalink, using the exact same
    `DISCUSSION_COMMENT_RE` `resolve_claimed_artifact` matches against. This
    exists so the hook's notion of "claimed" can never drift looser than
    what this module actually recognises: scanning raw text (rather than
    only the parsed JSON) would let an envelope that merely *mentions* a
    permalink in prose — without asserting it as a machine-readable field —
    trigger a network call, an audit row, and a team-log line over an
    incidental reference."""
    if not isinstance(envelope, dict):
        return None
    m = DISCUSSION_COMMENT_RE.search(json.dumps(envelope))
    return m.group(0) if m else None


def evaluate(tool_uses: int | None, sources_count: int, claimed_artifact: str | None = None) -> dict:
    """Run whichever checks apply and return the first non-null finding.

    Check 1 always runs (free, local, no network). Check 2 runs only when
    ``claimed_artifact`` is truthy — this is what keeps an envelope that
    claims no artifact from ever reaching the network (criterion 17).
    """
    result = check_impossible_sources(tool_uses, sources_count)
    if result.get("finding"):
        return result
    if claimed_artifact:
        artifact_result = resolve_claimed_artifact(claimed_artifact)
        if artifact_result.get("finding"):
            return artifact_result
    return result


def _state_dir() -> Path:
    from backend import state_paths  # noqa: PLC0415

    return Path(state_paths.AUDIT_LOG).parent


def _record_audit_row(finding: str, reason: str | None, role: str, discussion: str, pr: str) -> None:
    audit_dir = _state_dir()
    audit_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "kind": "envelope_fabrication_finding",
        "finding": finding,
        "reason": reason,
        "role": role or "unknown",
        "discussion": discussion or None,
        "pr": pr or None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with (audit_dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _record_team_log(finding: str, role: str, discussion: str, pr: str) -> None:
    import subprocess  # noqa: PLC0415

    script = os.environ.get("ENVELOPE_CHECK_TEAM_LOG_SCRIPT") or str(_REPO_ROOT / "scripts" / "rotate-team-log.sh")
    if not os.path.isfile(script):
        return
    msg = f"[envelope-check] finding={finding} role={role or 'unknown'}"
    if discussion:
        msg += f" D#{discussion}"
    if pr:
        msg += f" PR#{pr}"
    subprocess.run(  # noqa: S603
        ["bash", script, "comment", msg],
        timeout=15,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def record_finding(result: dict, role: str = "unknown", discussion: str = "", pr: str = "") -> None:
    """Write one audit.jsonl row and one team-log line for a non-null
    finding. No-op for a null finding. Never raises — a recording failure
    must not turn a detector into a crash on the post-agent-hook path."""
    finding = result.get("finding")
    if not finding:
        return
    try:
        _record_audit_row(finding, result.get("reason"), role, discussion, pr)
    except Exception as exc:  # noqa: BLE001
        print(f"[envelope_check] WARN: audit write failed: {exc}", file=sys.stderr)
    try:
        _record_team_log(finding, role, discussion, pr)
    except Exception as exc:  # noqa: BLE001
        print(f"[envelope_check] WARN: team-log write failed: {exc}", file=sys.stderr)


def _parse_tool_uses(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="D#1791 envelope fabrication detector")
    p.add_argument("--tool-uses", default="", help='tri-state: "" = unknown, or an int')
    p.add_argument("--sources-count", default="0")
    p.add_argument("--claimed-artifact", default="")
    p.add_argument("--json", action="store_true", help="accepted for interface clarity; always prints JSON")
    p.add_argument("--record", action="store_true", help="write audit.jsonl + team-log for a non-null finding")
    p.add_argument("--role", default="unknown")
    p.add_argument("--discussion", default="")
    p.add_argument("--pr", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        tool_uses = _parse_tool_uses(args.tool_uses)
        try:
            sources_count = int(args.sources_count)
        except (TypeError, ValueError):
            sources_count = 0
        claimed_artifact = args.claimed_artifact or None

        result = evaluate(tool_uses, sources_count, claimed_artifact)

        if args.record and result.get("finding"):
            record_finding(result, role=args.role, discussion=args.discussion, pr=args.pr)

        print(json.dumps({"finding": result.get("finding"), "reason": result.get("reason")}))
    except Exception as exc:  # noqa: BLE001 — a detector must never crash its caller
        print(json.dumps({"finding": None, "reason": f"internal_error: {exc.__class__.__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
