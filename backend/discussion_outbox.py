"""backend/discussion_outbox.py — the Discussion outbox, posted by the
SubagentStop hook (D#2615 ENG-0, correction C2).

The design (see the Discussion for the full Spec; restated here in our own
words against the code, per the repo-scope card):

An agent that wants to post a Discussion comment, or update a Discussion's
body, writes the text to a file in its own worktree instead of posting it
itself:

  - `.discussion-outbox/comment.md` — a comment to post.
  - `.discussion-outbox/body.md`    — the complete new Discussion body.

When the agent's turn ends, the SubagentStop hook (`hooks/discussion_outbox_
stop.py`) calls `process_stop_event` below, which:

  1. Resolves the stopping spawn's role and Discussion from the spawn
     registry (`backend/spawn_registry.py`) — never from anything the agent
     wrote.
  2. Applies the refusals (role allow-list, forged-identity check, body
     guards, file-shape checks).
  3. Posts through an injected GitHub client.
  4. Writes an audit line for every decision.

Threat model
------------
In scope: anything an agent can write into its outbox files, its final
message, or its AGENT_OUTPUT envelope. Out of scope: an agent that uses shell
access to write the registry, the ledger, the audit log, or its own
transcript directly — `hooks/` is a guardrail, not a security boundary
(CLAUDE.md), and this file doesn't claim otherwise.

Identity: the join key
-----------------------
The one thing available at SubagentStop time that the agent cannot forge is
the literal text of the FIRST message in its own transcript
(`agent_transcript_path` in the SubagentStop payload) — that message is the
prompt Team Lead passed to `Agent()`, assembled by `scripts/spawn-agent.sh`,
and it carries a `hook_event_id=<role>-<discussion-or-nod>-<unix-ts>` trailer
line (see `backend/prompt_builder.py`). `spawn-agent.sh` records that same
event id, with the role and Discussion it was given, in
`backend/spawn_registry.py` before the spawn starts.

This is deliberately NOT `scripts/lib/transcript_event_id.py`'s scan: that
module walks every `tool_result` in the transcript looking for the first
canonically-shaped tag, which is exactly the wrong shape here — a `tool_result`
is produced by the agent's own actions (e.g. reading a file that happens to
quote this very tag), so a tag recovered that way is something the agent
could influence. `_extract_join_key` below only ever looks at the FIRST
record in the transcript, and only at its `text`-type content blocks — never
a `tool_use`/`tool_result` block — which is the one part of the transcript
that is entirely caller-authored and impossible for the agent to edit.

Even within that first message, only a whole line matching
`^hook_event_id=<id>$` counts as the trailer — never a substring embedded
mid-sentence. `prompt_builder.py` always appends the real trailer as its own
line after the task text, so scanning for a standalone line and taking the
LAST one matches that placement; an earlier occurrence quoted inline (a log
line, a Discussion excerpt) is not itself a standalone line and never even
enters the running. If more than one DISTINCT id each appears on its own
line, `_extract_join_key` refuses rather than guesses, and the caller treats
that the same as `ambiguous_registry`.

Dedup, and why there is no trailer
------------------------------------
The Spec requires "the whole file is the text that gets posted" — no
frontmatter, header, or field is parsed out of either outbox file. That rules
out appending a synthetic tracking trailer (e.g. an HTML-comment ledger
marker) to the posted text, since the posted text would then no longer be
byte-identical to the file. Exactly-once posting is instead achieved by
comparing the *outbox file's own content* against what is already live on
GitHub: for a comment, against every existing comment's body; for a body
update, against the Discussion's current body. An identical match is
`duplicate` (no GitHub call); this is also what makes a crash between a
successful post and this hook's own bookkeeping safe — the next run finds
the same content already live and treats it as a duplicate rather than
double-posting.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from backend import state_paths
from backend.spawn_registry import find_by_event_id

MAX_BYTES = 65536
OUTBOX_DIRNAME = ".discussion-outbox"
SENT_DIRNAME = "sent"
COMMENT_FILENAME = "comment.md"
BODY_FILENAME = "body.md"

# Criterion 4: role allow-list.
COMMENT_ROLES = {
    "project-manager",
    "technical-architect",
    "product-owner",
    "cost-analyst",
    "performance-expert",
    "security-expert",
    "researcher",
    "ux-designer",
}
BODY_ROLES = {"project-manager"}

_ALLOWED_BODY_PHASES = {"DISCUSSING", "CONSENSUS", "SPEC_READY"}

# Matches a STATUS comment's line 1, e.g.:
#   <!-- STATUS:SPEC_READY SINCE:2026-09-27T00:00:00Z BLOCKED-BY:#123,D#456 -->
_STATUS_LINE_RE = re.compile(
    r'^<!--\s*STATUS:(?P<phase>[A-Z_]+)\s+SINCE:(?P<since>\S+)'
    r'(?:\s+BLOCKED-BY:(?P<blocked>\S+))?\s*-->'
)

# Same envelope shape scripts/lib/subagent_payload.py's ENVELOPE_RE uses.
_ENVELOPE_RE = re.compile(
    r'<!--\s*AGENT_OUTPUT\s*-->\s*```json\s*(.*?)\s*```\s*<!--\s*/AGENT_OUTPUT\s*-->',
    re.DOTALL,
)

# The canonical hook_event_id shape scripts/spawn-agent.sh:EVENT_ID builds:
# "${ROLE}-${DISCUSSION:-nod}-$(date +%s)". Deliberately restricted to the
# FIRST message's plain-text content only — see _extract_join_key. Anchored
# to a WHOLE line (re.MULTILINE, ^...$) so an id quoted mid-sentence earlier
# in the task text (a log line, a Discussion excerpt) never matches — only
# the real trailer prompt_builder.py appends as its own line does.
_EVENT_ID_LINE_RE = re.compile(
    r'^hook_event_id=([a-z]+(?:-[a-z]+)*-(?:[0-9]+|nod)-[0-9]{9,12})$',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Identity resolution (criteria 2, 8, 13, 14)
# ---------------------------------------------------------------------------


def _is_linked_worktree(cwd: str) -> bool:
    """True if *cwd* is a linked git worktree (a `.git` pointer file), never
    the main checkout (a `.git` directory, shared by every concurrent agent)
    — same check CLAUDE.md's own HARD STOP section uses (`cat .git`)."""
    if not cwd:
        return False
    git_path = Path(cwd) / ".git"
    try:
        if git_path.is_dir():
            return False
        if git_path.is_file():
            head = git_path.read_text(encoding="utf-8", errors="replace")
            return head.strip().startswith("gitdir:")
        return False
    except OSError:
        return False


def _first_message_text(agent_transcript_path: str) -> str:
    """Return the literal text of the FIRST message in *agent_transcript_path*
    — never a `tool_result` payload, and never any later line. This is the
    one part of the transcript the calling process (Team Lead, via
    `scripts/spawn-agent.sh`'s assembled prompt) writes and the agent itself
    cannot alter. Returns "" on anything unreadable or unexpected — never
    raises."""
    if not agent_transcript_path:
        return ""
    try:
        with open(agent_transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline()
    except OSError:
        return ""
    first_line = first_line.strip()
    if not first_line:
        return ""
    try:
        obj = json.loads(first_line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""

    role = obj.get("role", "")
    content = obj.get("content", "")
    if not role and isinstance(obj.get("message"), dict):
        msg = obj["message"]
        role = msg.get("role", "")
        content = msg.get("content", "")

    if role != "user":
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Only genuine "text" blocks — never a tool_use/tool_result block,
        # which is agent-influenced.
        chunks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "\n".join(chunks)
    return ""


def _extract_join_key(agent_transcript_path: str) -> tuple[str, bool]:
    """Returns (event_id, ambiguous).

    Only a whole line matching `^hook_event_id=<id>$` counts (see the module
    docstring) — never a substring embedded mid-sentence. Uses the LAST such
    line, matching where `prompt_builder.py` appends the real trailer (after
    all task text), so an earlier, unrelated standalone occurrence can never
    win over it. `ambiguous` is True when more than one DISTINCT id each
    appears on its own line — refuse rather than guess which one is real.
    """
    text = _first_message_text(agent_transcript_path)
    if not text:
        return "", False
    matches = _EVENT_ID_LINE_RE.findall(text)
    if not matches:
        return "", False
    if len(set(matches)) > 1:
        return "", True
    return matches[-1], False


def _resolve_identity(payload: dict) -> tuple[Optional[dict], Optional[str]]:
    """Returns (registry_entry, refusal_reason) — exactly one is not None.

    Refusal reasons match criterion 13 exactly: `not_worktree`,
    `no_registry_entry`, `ambiguous_registry`, `role_mismatch`. Every
    refusal here also prints one warning line to stderr (criterion 13) —
    a misconfigured spawn is otherwise silent at the terminal.
    """
    cwd = payload.get("cwd") or ""
    if not _is_linked_worktree(cwd):
        return None, _warn_identity_refusal("not_worktree")

    agent_transcript_path = payload.get("agent_transcript_path") or ""
    event_id, ambiguous = _extract_join_key(agent_transcript_path)
    if ambiguous:
        return None, _warn_identity_refusal("ambiguous_registry")
    if not event_id:
        return None, _warn_identity_refusal("no_registry_entry")

    entries = find_by_event_id(event_id)
    if not entries:
        return None, _warn_identity_refusal("no_registry_entry")
    if len(entries) > 1:
        return None, _warn_identity_refusal("ambiguous_registry")

    entry = entries[0]
    agent_type = payload.get("agent_type") or ""
    if entry.get("role") != agent_type:
        return None, _warn_identity_refusal("role_mismatch")

    return entry, None


def _warn_identity_refusal(reason: str) -> str:
    """Criterion 13: an identity refusal prints one stderr warning line, in
    addition to the audit row `_process_one_file` writes. Never raises."""
    try:
        print(f"discussion_outbox: refused: {reason}", file=sys.stderr)
    except OSError:
        pass
    return reason


def _in_hook_context(payload: dict, repo_root) -> bool:
    """True only when this call plausibly reached `process_stop_event`
    through a real Claude Code SubagentStop hook invocation, never a
    hand-run script, `python3 -c`, or a pytest subprocess (D#2626).

    `CLAUDE_PROJECT_DIR` is set by Claude Code for hook commands (see
    `.claude/settings.json`, which runs this hook as
    `python3 $CLAUDE_PROJECT_DIR/hooks/discussion_outbox_stop.py`) and is
    absent from a sub-agent's own Bash environment — measured directly, not
    assumed — along with anything a subprocess it starts inherits. Comparing
    it against `repo_root` (rather than merely checking it is set) also
    catches a value left over from a different checkout. `hook_event_name`
    is a cheap second check for a hand-built fixture that omits the field.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    if not project_dir:
        return False
    try:
        if Path(project_dir).resolve() != Path(repo_root).resolve():
            return False
    except OSError:
        return False
    return payload.get("hook_event_name") == "SubagentStop"


def _warn_hook_context_refusal() -> str:
    """A hand-run refusal prints one stderr warning line, same shape as
    `_warn_identity_refusal`. Never raises."""
    try:
        print("discussion_outbox: refused: not_hook_context", file=sys.stderr)
    except OSError:
        pass
    return "not_hook_context"


# ---------------------------------------------------------------------------
# File validation (criterion 11)
# ---------------------------------------------------------------------------


def _validate_outbox_file(path: Path, worktree_root: Path) -> tuple[Optional[bytes], Optional[str]]:
    """Returns (content_bytes, None) on success, or (None, reason)."""
    try:
        if path.is_symlink():
            return None, "not_regular_file"
        resolved = path.resolve()
        try:
            resolved.relative_to(worktree_root.resolve())
        except ValueError:
            return None, "not_regular_file"
        if not path.is_file():
            return None, "not_regular_file"
        if path.stat().st_size > MAX_BYTES:
            return None, "oversize"
        data = path.read_bytes()
    except OSError:
        return None, "not_regular_file"

    if b"\x00" in data:
        return None, "nul_byte"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not_utf8"
    if not text.strip():
        return None, "empty"
    return data, None


# ---------------------------------------------------------------------------
# Role allow-list and forged-identity checks (criteria 4, 5)
# ---------------------------------------------------------------------------


def _check_role_allowed(kind: str, role: str) -> Optional[str]:
    allowed = COMMENT_ROLES if kind == "comment" else BODY_ROLES
    return None if role in allowed else "role_not_allowed"


def _find_envelope_blocks(text: str) -> list[Optional[dict]]:
    """One entry per AGENT_OUTPUT block: the parsed dict, or None if that
    block's JSON failed to parse (still "a block that can't be checked")."""
    out: list[Optional[dict]] = []
    for raw in _ENVELOPE_RE.findall(text):
        try:
            parsed = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            out.append(None)
            continue
        out.append(parsed if isinstance(parsed, dict) else None)
    return out


def _check_forged_panel_seat(text: str, role: str) -> Optional[str]:
    blocks = _find_envelope_blocks(text)
    if not blocks:
        return None
    agents_named = set()
    for block in blocks:
        if block is None:
            return "forged_envelope"
        agents_named.add(block.get("agent"))
    if len(agents_named) > 1:
        return "forged_envelope"
    if next(iter(agents_named)) != role:
        return "forged_envelope"
    return None


# ---------------------------------------------------------------------------
# Body guards (criterion 6)
# ---------------------------------------------------------------------------


def _parse_status_line(line: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (phase, blocked_by) for a STATUS comment line, or (None, None)."""
    m = _STATUS_LINE_RE.match(line.strip())
    if not m:
        return None, None
    return m.group("phase"), m.group("blocked")


def _check_body_guards(current_body: str, new_body: str) -> Optional[str]:
    new_lines = new_body.splitlines()
    new_line1 = new_lines[0] if new_lines else ""
    phase, _new_blocked = _parse_status_line(new_line1)
    if phase not in _ALLOWED_BODY_PHASES:
        return "bad_status_phase"

    current_lines = current_body.splitlines()
    current_line1 = current_lines[0] if current_lines else ""
    _cur_phase, cur_blocked = _parse_status_line(current_line1)
    _new_phase, new_blocked = _parse_status_line(new_line1)
    if cur_blocked and not new_blocked:
        return "blocked_by_dropped"
    return None


# ---------------------------------------------------------------------------
# Audit (criterion 7)
# ---------------------------------------------------------------------------


def _outcome_row(agent_id, role, discussion, kind, sha256_hex, outcome) -> dict:
    return {
        "kind": "discussion_outbox",
        "agent_id": agent_id,
        "role": role,
        "discussion": discussion,
        "file": kind,
        "sha256": sha256_hex,
        "outcome": outcome,
    }


def _refused_row(agent_id, role, discussion, kind, sha256_hex, reason) -> dict:
    return _outcome_row(agent_id, role, discussion, kind, sha256_hex, f"refused:{reason}")


def _append_audit(row: dict) -> None:
    try:
        state_paths.ensure_state_dir()
        with open(state_paths.AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _move_to_sent(outbox_dir: Path, src: Path, sha256_hex: str) -> None:
    try:
        sent_dir = outbox_dir / SENT_DIRNAME
        sent_dir.mkdir(parents=True, exist_ok=True)
        src.replace(sent_dir / f"{sha256_hex}.md")
    except OSError:
        pass  # never blocks a stop (criterion 15)


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Injected client interface — tests supply a fake; production uses
    RealGitHubClient. No agent ever sees this class; it is only ever
    instantiated inside the hook process (criterion 3)."""

    def get_discussion(self, repo: str, number: int) -> dict:
        raise NotImplementedError

    def list_comments(self, repo: str, number: int) -> list[dict]:
        raise NotImplementedError

    def create_comment(self, repo: str, number: int, body: str) -> dict:
        raise NotImplementedError

    def update_body(self, repo: str, number: int, body: str) -> dict:
        raise NotImplementedError


class RealGitHubClient(GitHubClient):
    """Shells out to `gh api graphql`. Every call has a hard 30s timeout
    (criterion 15) — never exercised by the hermetic test suite."""

    _TIMEOUT_SECONDS = 30

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=self._TIMEOUT_SECONDS,
            check=True,
        )
        return result.stdout

    def get_discussion(self, repo: str, number: int) -> dict:
        owner, name = repo.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$number:Int!){ "
            "repository(owner:$owner,name:$name){ discussion(number:$number){ "
            "id body } } }"
        )
        out = self._run(
            [
                "api", "graphql",
                "-f", f"query={query}",
                "-f", f"owner={owner}",
                "-f", f"name={name}",
                "-F", f"number={number}",
            ]
        )
        data = json.loads(out)
        disc = data["data"]["repository"]["discussion"]
        return {"id": disc["id"], "body": disc.get("body") or ""}

    def list_comments(self, repo: str, number: int) -> list[dict]:
        owner, name = repo.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){ "
            "repository(owner:$owner,name:$name){ discussion(number:$number){ "
            "comments(first:100, after:$after){ nodes{ body } "
            "pageInfo{ hasNextPage endCursor } } } } }"
        )
        comments: list[dict] = []
        after = None
        while True:
            args = [
                "api", "graphql",
                "-f", f"query={query}",
                "-f", f"owner={owner}",
                "-f", f"name={name}",
                "-F", f"number={number}",
            ]
            if after:
                args += ["-f", f"after={after}"]
            out = self._run(args)
            data = json.loads(out)
            conn = data["data"]["repository"]["discussion"]["comments"]
            comments.extend({"body": n.get("body") or ""} for n in conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        return comments

    def create_comment(self, repo: str, number: int, body: str) -> dict:
        disc = self.get_discussion(repo, number)
        mutation = (
            "mutation($id:ID!,$body:String!){ addDiscussionComment(input:"
            "{discussionId:$id, body:$body}){ comment{ id } } }"
        )
        out = self._run(
            ["api", "graphql", "-f", f"query={mutation}", "-f", f"id={disc['id']}", "-f", f"body={body}"]
        )
        return json.loads(out)

    def update_body(self, repo: str, number: int, body: str) -> dict:
        disc = self.get_discussion(repo, number)
        mutation = (
            "mutation($id:ID!,$body:String!){ updateDiscussion(input:"
            "{discussionId:$id, body:$body}){ discussion{ id } } }"
        )
        out = self._run(
            ["api", "graphql", "-f", f"query={mutation}", "-f", f"id={disc['id']}", "-f", f"body={body}"]
        )
        return json.loads(out)


def _resolve_discussion_repo(repo_root: Path) -> str:
    """The Discussion-plane repo slug, read only from `.autonomous-team/
    project.json`'s `repo` key — never `code_repo` (criterion 2)."""
    project_json = Path(repo_root) / ".autonomous-team" / "project.json"
    try:
        data = json.loads(project_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    repo = data.get("repo") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) else ""


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _process_comment(text: str, role: str, discussion, client: GitHubClient, repo: str, sha: str, agent_id: str) -> dict:
    reason = _check_forged_panel_seat(text, role)
    if reason:
        return _refused_row(agent_id, role, discussion, "comment", sha, reason)

    try:
        existing = client.list_comments(repo, discussion)
        is_dup = any(c.get("body") == text for c in existing)
    except Exception:
        return _outcome_row(agent_id, role, discussion, "comment", sha, "post_failed")

    if is_dup:
        return _outcome_row(agent_id, role, discussion, "comment", sha, "duplicate")

    try:
        client.create_comment(repo, discussion, text)
    except Exception:
        return _outcome_row(agent_id, role, discussion, "comment", sha, "post_failed")

    return _outcome_row(agent_id, role, discussion, "comment", sha, "posted")


def _process_body(text: str, role: str, discussion, client: GitHubClient, repo: str, sha: str, agent_id: str) -> dict:
    try:
        disc = client.get_discussion(repo, discussion)
    except Exception:
        return _outcome_row(agent_id, role, discussion, "body", sha, "post_failed")

    current_body = disc.get("body") or ""
    if current_body == text:
        return _outcome_row(agent_id, role, discussion, "body", sha, "duplicate")

    reason = _check_body_guards(current_body, text)
    if reason:
        return _refused_row(agent_id, role, discussion, "body", sha, reason)

    try:
        client.update_body(repo, discussion, text)
    except Exception:
        return _outcome_row(agent_id, role, discussion, "body", sha, "post_failed")

    return _outcome_row(agent_id, role, discussion, "body", sha, "posted")


def _process_one_file(
    kind: str,
    path: Path,
    worktree_root: Path,
    agent_id: str,
    identity_entry: Optional[dict],
    identity_reason: Optional[str],
    client: GitHubClient,
    repo: str,
) -> dict:
    content, reason = _validate_outbox_file(path, worktree_root)
    if reason:
        # sha256 of whatever bytes ARE on disk, best-effort, purely for the
        # audit trail — never gates the refusal.
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            sha = ""
        return _refused_row(agent_id, None, None, kind, sha, reason)

    sha = hashlib.sha256(content).hexdigest()
    text = content.decode("utf-8")

    if identity_reason:
        return _refused_row(agent_id, None, None, kind, sha, identity_reason)

    role = identity_entry.get("role") if identity_entry else None
    discussion = identity_entry.get("discussion") if identity_entry else None

    reason = _check_role_allowed(kind, role)
    if reason:
        return _refused_row(agent_id, role, discussion, kind, sha, reason)

    if kind == "comment":
        row = _process_comment(text, role, discussion, client, repo, sha, agent_id)
    else:
        row = _process_body(text, role, discussion, client, repo, sha, agent_id)

    if row["outcome"] in ("posted", "duplicate"):
        _move_to_sent(path.parent, path, sha)

    return row


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def process_stop_event(payload: dict, repo_root, client: Optional[GitHubClient] = None) -> list[dict]:
    """Called by `hooks/discussion_outbox_stop.py` on every SubagentStop.

    Returns the list of audit rows written (useful for tests); the hook
    itself ignores the return value and always exits 0. Never raises past
    this function — every internal step is already wrapped, and the caller
    additionally wraps this whole call.
    """
    cwd = payload.get("cwd") or ""
    if not cwd:
        return []

    outbox_dir = Path(cwd) / OUTBOX_DIRNAME
    present: list[tuple[str, Path]] = []
    for kind, filename in (("comment", COMMENT_FILENAME), ("body", BODY_FILENAME)):
        candidate = outbox_dir / filename
        # is_symlink() first: .exists() alone follows symlinks and returns
        # False for a DANGLING one, so it would never even reach
        # _validate_outbox_file's own not_regular_file check below — silently
        # skipped, with no audit line at all.
        if candidate.is_symlink() or candidate.exists():
            present.append((kind, candidate))

    if not present:
        # Criterion 7: absent/empty outbox writes no audit line, makes no
        # GitHub call. This is the case for every executor/reviewer stop.
        return []

    agent_id = payload.get("agent_id") or ""
    identity_entry, identity_reason = _resolve_identity(payload)

    if client is None:
        if not _in_hook_context(payload, repo_root):
            reason = _warn_hook_context_refusal()
            rows = []
            for kind, path in present:
                try:
                    sha = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    sha = ""
                row = _refused_row(agent_id, None, None, kind, sha, reason)
                _append_audit(row)
                rows.append(row)
            return rows
        client = RealGitHubClient()
    repo = _resolve_discussion_repo(repo_root)

    rows = []
    for kind, path in present:
        row = _process_one_file(kind, path, Path(cwd), agent_id, identity_entry, identity_reason, client, repo)
        _append_audit(row)
        rows.append(row)
    return rows
