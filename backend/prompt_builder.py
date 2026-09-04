"""backend/prompt_builder.py — Typed Python prompt builder for agent spawns.

Replaces the bash heredoc/eval prompt assembly in scripts/spawn-agent.sh with
a dataclass that assembles sections in a canonical, tested order.

Usage (library):
    from backend.prompt_builder import SpawnPrompt, build_from_psc
    prompt = SpawnPrompt(
        role="executor",
        discussion=961,
        task_prompt="implement the thing",
        persona_voice="## Voice\\n...",
        working_principles="...",
        self_observe_gate="...",
        gate_line="[Control plane gates: ...]",
        hook_event_id="executor-961-1234567890",
        prompt_manifest={"manifest": "executor.tmpl@abc123"},
    ).render()

Usage (CLI):
    echo '{"role":"executor","discussion":961,"task_prompt":"...","hook_event_id":"..."}' \\
      | python3 -m backend.prompt_builder render

The CLI reads JSON from stdin and writes the assembled prompt to stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Max characters for the injected previous-attempt context section (~500 tokens).
_PREV_CONTEXT_MAX_CHARS = 2000

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_TEMPLATES_DIR = _BACKEND_DIR / "spawn_templates"
_FRAGMENTS_DIR = _TEMPLATES_DIR / "fragments"
_CHECKLIST_SH = _SCRIPTS_DIR / "lib" / "pre-code-checklist.sh"

# ---------------------------------------------------------------------------
# Static security-block text (moved from spawn-agent.sh --security-trigger)
# ---------------------------------------------------------------------------

_SECURITY_BLOCK = (
    "SECURITY CONTEXT: This spawn was triggered by a security-sensitive code change.\n"
    "Before reviewing, read the full diff carefully for:\n"
    "  - Auth bypass patterns (e.g. skipped token validation, hardcoded credentials)\n"
    "  - SQL injection via string interpolation\n"
    "  - Secret exfiltration (logging, env var leaks, webhook payloads)\n"
    "  - Sandbox escape via subprocess / exec calls\n"
    "  - Insecure deserialization, path traversal, SSRF\n"
    "Emit a security-specific findings block in your AGENT_OUTPUT even if verdict=pass."
)

# ---------------------------------------------------------------------------
# Unprovisioned-worktree block (D#2014)
# ---------------------------------------------------------------------------

# Roles that only ever read/verify a tree, never edit it — the only roles for
# which scripts/lib/verify-tree.sh's verify_tree_build is a usable substitute
# when no writable worktree was provisioned. Sourced from the roles that
# already document it as their sanctioned mechanism (.claude/agents/{role}.md).
# Do not add a role here without it: verify_tree_build write-protects every
# tracked file (chmod a-w) and clones with origin pointed at the local parent
# checkout, not GitHub — unusable for anything that needs to edit or push.
_PR_TREE_READONLY_ROLES = frozenset({"code-reviewer", "acceptance-tester"})


# D#2222: reason string set by scripts/spawn-agent.sh alongside
# worktree_unprovisioned=True, distinguishing WHY no concrete path was
# resolved. This is a genuine three-way distinction, not a binary — a PR
# code-review finding on the original D#2222 fix caught the binary version
# collapsing a --pr resolution failure into the reassuring "canonical
# fresh spawn" case, which would have told the agent it was safe to proceed
# in a tree that is definitionally wrong for a PR amend:
#
#   "" (default/unknown) or "pr_tree_failed": a real provisioning attempt
#       was made (pr_tree_provision ran against a resolved head sha) and it
#       failed — the honest answer is a hard stop, no fallback tree exists.
#   "pr_resolution_failed": --pr was given, but the head sha/branch could
#       not even be resolved (gh api failure), so pr_tree_provision was
#       never attempted. This must ALSO hard-fail, and must NOT be treated
#       like the canonical case below: whatever tree the Agent tool's own
#       isolation hands the agent is not the PR's branch, so proceeding
#       would silently amend the wrong tree.
#   "agent_tool_provisions": no provisioning was attempted at all, because
#       none was spawn-agent.sh's to attempt: --isolation worktree was
#       passed with no --pr and no --worktree-path, which is the canonical
#       fresh-spawn shape (see scripts/lib/team-lead-prompts.sh) — the
#       Agent() tool call's own isolation="worktree" param provisions the
#       real tree, a step spawn-agent.sh has no visibility into since it
#       only assembles prompt text. Telling the agent to hard-fail in this
#       one case was the original D#2222 bug: the canonical spawn shape was
#       self-reporting as broken every time.
_WT_REASON_AGENT_TOOL_PROVISIONS = "agent_tool_provisions"
_WT_REASON_PR_RESOLUTION_FAILED = "pr_resolution_failed"


def _build_unprovisioned_worktree_block(role: str, reason: str = "") -> str:
    """Block emitted when worktree isolation was requested but spawn-agent.sh
    resolved no concrete path — replaces the old self-contradictory claim
    (asserting a worktree at a literal, unexpanded "$(pwd)" string).

    Deliberately contains no "YOUR WORKTREE" line: that phrase is reserved
    for a real, provisioned path (see _build_worktree_block above).
    """
    if reason == _WT_REASON_AGENT_TOOL_PROVISIONS:
        return "\n".join([
            "spawn-agent.sh did not pre-provision a worktree for this spawn — that's",
            "expected here. isolation=\"worktree\" was requested with no --pr, so the real",
            "tree comes from the Agent tool's own isolation param on the Agent() call that",
            "launched you, which this script has no visibility into.",
            "",
            "Run: pwd",
            f"If that prints {_REPO_ROOT} (the repo root, not a worktree) — isolation was",
            "NOT applied to your spawn. STOP: do not improvise a tree with checkout/switch/",
            "branch/reset/restore/worktree or any other git verb; those are hard-blocked by",
            'design. Emit verdict: fail with block_reason: "worktree_not_provisioned" in',
            "your AGENT_OUTPUT and stop.",
            "",
            "If pwd prints anything else, that IS your provisioned worktree root — confirm",
            "with `git branch --show-current` (should not be main/master), then proceed normally",
            "using this directory as the absolute prefix for every Edit/Write call.",
        ])

    if reason == _WT_REASON_PR_RESOLUTION_FAILED:
        return "\n".join([
            "NO WORKTREE WAS PROVISIONED for this spawn.",
            "This is a --pr amend spawn, but the PR's head branch/sha could not be",
            "resolved (a gh api call failed — see the spawn's stderr WARN). No tree was",
            "provisioned as a result, and there is no safe fallback: this spawn has no",
            "way to know which branch it should be amending, so ANY tree you land in —",
            "including one the Agent tool auto-provisions off main — is NOT the PR's",
            "branch. Proceeding would silently amend the wrong tree.",
            "",
            "Run: pwd",
            "Do not improvise a tree with checkout/switch/branch/reset/restore/worktree",
            "or any other git verb to reach one; those are hard-blocked by design and",
            "any workaround is a guardrail bypass, not a fix. Emit verdict: fail with",
            'block_reason: "worktree_not_provisioned" in your AGENT_OUTPUT and stop.',
        ])

    lines = [
        "NO WORKTREE WAS PROVISIONED for this spawn.",
        "Run: pwd",
        f"If that prints {_REPO_ROOT} (the repo root, not a worktree) — STOP.",
        "Do not improvise a tree with checkout/switch/branch/reset/restore/worktree",
        "or any other git verb to reach one; those are hard-blocked by design and",
        "any workaround is a guardrail bypass, not a fix. Emit verdict: fail with",
        'block_reason: "worktree_not_provisioned" in your AGENT_OUTPUT and stop.',
    ]
    if role in _PR_TREE_READONLY_ROLES:
        lines += [
            "",
            "For read-only inspection of a PR head, the sanctioned mechanism is",
            "scripts/lib/verify-tree.sh's verify_tree_build (source it, then call",
            "verify_tree_build <sha> <dest>). See its file header for why",
            "`git clone --shared --revision=<sha>` is used instead of clone-then-",
            "checkout — the second half of that two-step form is blocked at",
            "hooks/sandbox_rules.py:2590 at every tier.",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checklist helpers
# ---------------------------------------------------------------------------

def _build_previous_attempt_context(discussion: int | None) -> str:
    """Look up prior failure records for *discussion* and format a context block.

    Returns an empty string when:
    - discussion is None
    - no failures have been recorded for this Discussion
    - imports fail (circuit_breaker/agent_retros unavailable)

    The section is capped at _PREV_CONTEXT_MAX_CHARS. Lines are trimmed at the
    last complete line before the limit and ``...`` is appended.
    """
    if discussion is None:
        return ""

    try:
        from backend.circuit_breaker import get_latest_failure
        from backend.agent_retros import get_latest_retro
    except ImportError:
        return ""

    failure = get_latest_failure(discussion)
    if failure is None:
        return ""

    lines: list[str] = [
        "### Previous Attempt Context",
        "",
        f"Failure count: {failure.get('count', '?')}",
    ]

    reason = failure.get("reason")
    if reason:
        lines.append(f"Last failure reason: {reason}")

    retro = get_latest_retro(discussion)
    if retro:
        lines.append("")
        lines.append("Last retro:")
        if retro.get("classifier"):
            lines.append(f"  classifier: {retro['classifier']}")
        if retro.get("trigger"):
            lines.append(f"  trigger: {retro['trigger']}")
        if retro.get("why"):
            lines.append(f"  why: {retro['why']}")
        if retro.get("future_fix"):
            lines.append(f"  future_fix: {retro['future_fix']}")

    lines.append("")
    lines.append("Do not repeat the same approach that caused the previous failure.")

    section = "\n".join(lines)

    # Cap at _PREV_CONTEXT_MAX_CHARS — trim at last complete line before limit.
    if len(section) > _PREV_CONTEXT_MAX_CHARS:
        truncated = section[: _PREV_CONTEXT_MAX_CHARS]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            section = truncated[:last_nl] + "\n..."
        else:
            section = truncated + "..."

    return section


def _load_checklist_block(role: str) -> str:
    """Load the per-role checklist block via pre-code-checklist.sh.

    Returns empty string if the shell lib is absent or the role has no checklist.
    Never raises.
    """
    if not _CHECKLIST_SH.exists():
        return ""
    fn_map = {
        "executor": "pre_code_checklist_block",
        "code-reviewer": "code_reviewer_checklist_block",
    }
    fn = fn_map.get(role)
    if not fn:
        return ""
    try:
        result = subprocess.run(
            ["bash", str(_CHECKLIST_SH), fn],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# Template body helpers
# ---------------------------------------------------------------------------

def _pr_url(pr: int) -> str:
    from backend._repo import REPO
    return f"https://github.com/{REPO}/pull/{pr}"


def _discussion_url(discussion: int) -> str:
    from backend._repo import REPO
    return f"https://github.com/{REPO}/discussions/{discussion}"


def _load_template_body(
    role: str,
    task_brief: str = "",
    discussion_number: str = "",
    discussion: int | None = None,
    pr: int | None = None,
    pr_branch: str = "",
) -> tuple[str, dict]:
    """Load the rendered template body for *role* using spawn_templates.render_body().

    Returns (body_text, manifest_dict).

    Unknown role returns ("", {}) — that's a caller error the spawn already
    guards against elsewhere, not a rendering failure. Anything else
    (a contract violation naming a missing variable, a substitution error)
    propagates so the caller sees it — D#1788: this used to be a blanket
    ``except Exception: return "", {}`` that swallowed exactly the error
    the new pr_number contract is supposed to surface.
    """
    from backend.spawn_templates import render_body, KNOWN_ROLES
    if role not in KNOWN_ROLES:
        return "", {}

    template_vars: dict[str, str] = {
        "task_brief": task_brief,
        "discussion_number": discussion_number,
    }
    if discussion is not None:
        template_vars["discussion_url"] = _discussion_url(discussion)
    if pr is not None:
        template_vars["pr_number"] = str(pr)
        template_vars["pr_branch"] = pr_branch
        template_vars["pr_url"] = _pr_url(pr)

    body, manifest = render_body(
        role,
        template_vars,
        ignore_unknown=False,
        return_manifest=True,
    )
    return body, manifest


# ---------------------------------------------------------------------------
# SpawnPrompt dataclass
# ---------------------------------------------------------------------------


@dataclass
class SpawnPrompt:
    """Typed container for all sections of an agent spawn prompt.

    Call .render() to assemble the canonical prompt string.
    Empty-string fields produce NO output — not even a stray section header.
    """

    role: str = ""
    discussion: int | None = None
    task_prompt: str = ""
    persona_voice: str = ""
    working_principles: str = ""
    self_observe_gate: str = ""
    gate_line: str = ""
    worktree_path: str | None = None
    # D#2014: set when isolation="worktree" was requested but no concrete path
    # could be resolved (e.g. a --pr amend spawn where pr_tree_provision
    # failed). Ignored when worktree_path is set — a real path always wins.
    worktree_unprovisioned: bool = False
    # D#2222: distinguishes WHY, so the emitted block can tell a real
    # provisioning failure (hard stop) apart from the canonical fresh-spawn
    # shape, where the Agent tool's own isolation param provisions the real
    # tree and no failure occurred at all. See _build_unprovisioned_worktree_block.
    worktree_unprovisioned_reason: str = ""
    security_block: bool = False
    hook_event_id: str = ""
    prompt_manifest: dict = field(default_factory=dict)

    # D#1788: PR number + head branch, threaded into the template body's
    # {{pr_number}} / {{pr_url}} / {{pr_branch}} slots. None means "no PR
    # context" (most roles) — a role whose template references {{pr_number}}
    # without one gets a loud SpawnVarContractError, not a blank slot.
    pr: int | None = None
    pr_branch: str = ""

    # ---- additional fields from PSC / template loading ----

    # Overrides the template body — if not None, skip loading from spawn_templates.
    # Used in tests to inject a known body. Empty string is a valid override (no body).
    _template_body_override: str | None = field(default=None, repr=False)

    # Overrides the checklist block — if not None, skip loading from pre-code-checklist.sh.
    # Used in tests to inject a known block. Empty string is a valid override (no checklist).
    _checklist_block_override: str | None = field(default=None, repr=False)

    # D#1956: accepted for payload-shape compatibility with spawn_payload.py,
    # but never rendered — the prompt-injection scrub lane was removed
    # (denied by the permission classifier; would only have protected a
    # single fresh-shell Bash call anyway). The real scrub is process-level,
    # in scripts/spawn-agent.sh, before the subagent is launched.
    env_scrub_snippet: str = ""

    # Prior test-run artifact block (from pr-artifacts.sh / --pr flag)
    prior_test_runs_block: str = ""

    # Dial state at spawn time — injected into volatile header so executors can
    # append "Dial state at spawn: <class>=<verb>, ..." footer to PR descriptions.
    # Empty string means dial registry was unavailable (non-blocking).
    dial_state_at_spawn: str = ""

    def _build_worktree_block(self) -> str:
        """Produce the WORKTREE PATH block for worktree-isolated spawns."""
        if not self.worktree_path:
            if self.worktree_unprovisioned:
                return _build_unprovisioned_worktree_block(
                    self.role, self.worktree_unprovisioned_reason
                )
            return ""
        wt = self.worktree_path
        return (
            f"YOUR WORKTREE: {wt}\n"
            "Use this path as the absolute prefix for EVERY Edit/Write call.\n"
            f"Never write to {_REPO_ROOT}/<file> — that is main, not your worktree.\n"
            "All file paths passed to Edit or Write MUST start with this worktree path.\n"
            "Before your first Edit/Write, run: pwd   to confirm your absolute worktree root.\n"
            "\n"
            "IMPORTANT — state symlinks: run this as your very first Bash step to ensure\n"
            ".autonomous-team/ state files point to the shared external state dir (not forked copies):\n"
            "  bash scripts/setup-state-dir.sh\n"
            "This is idempotent — safe to run every session."
        )

    def render(self) -> str:
        """Assemble the spawn prompt in canonical section order.

        Section order (matches current spawn-agent.sh PARTS assembly):
          1. TEMPLATE_BODY   — role template body (stable, cache-friendly)
          2. CHECKLIST_BLOCK — executor Pre-Code Checklist or code-reviewer enforcement
          3. PERSONA_VOICE   — from pre-spawn-check
          4. WORKING_PRINCIPLES — from pre-spawn-check
          5. SELF_OBSERVE_GATE  — from pre-spawn-check
          --- VOLATILE_BOUNDARY ---
          6. SECURITY_BLOCK — if security_block=True
          7. WORKTREE_BLOCK  — if worktree_path is set (provisioned path), else
                               the unprovisioned block if worktree_unprovisioned=True
          8. PRIOR_TEST_RUNS_BLOCK — if prior_test_runs_block is set
          9. PREVIOUS_ATTEMPT_CONTEXT — if circuit breaker has failures for this Discussion
         10. TASK_PROMPT     — the actual work description
         11. GATE_LINE       — current control-plane gate values
         12. hook_event_id line
         13. prompt_manifest line

        D#1956: the env-scrub prompt-injection block used to render here, right
        after VOLATILE_BOUNDARY. It was removed — it was denied by the
        permission classifier on every observed spawn, and even a permitted
        `unset` would only have protected the single Bash call it ran in
        (fresh shell per call). `env_scrub_snippet` is still accepted on the
        dataclass for payload-shape compatibility but is never rendered; the
        actual scrub is process-level, in scripts/spawn-agent.sh, before the
        subagent is ever launched.

        Empty sections produce NO output — not even a stray blank line pair.
        """
        # --- Stable prefix ---

        if self._template_body_override is not None:
            template_body = self._template_body_override
        else:
            template_body, loaded_manifest = _load_template_body(
                self.role,
                task_brief=self.task_prompt,
                discussion_number=str(self.discussion) if self.discussion else "",
                discussion=self.discussion,
                pr=self.pr,
                pr_branch=self.pr_branch,
            )
            # Thread the manifest through: use loaded value when caller didn't supply one.
            # This preserves the old bash behaviour where PROMPT_MANIFEST came from
            # spawn_templates.render_body() and was always emitted at end of prompt.
            if not self.prompt_manifest and loaded_manifest:
                self.prompt_manifest = loaded_manifest

        if self._checklist_block_override is not None:
            checklist_block = self._checklist_block_override
        else:
            checklist_block = _load_checklist_block(self.role)

        parts: list[str] = []

        if template_body:
            parts.append(template_body)
        if checklist_block:
            parts.append(checklist_block)
        if self.persona_voice:
            parts.append(self.persona_voice)
        if self.working_principles:
            parts.append(self.working_principles)
        if self.self_observe_gate:
            parts.append(self.self_observe_gate)

        # --- Volatile boundary (always present) ---
        parts.append("<!-- VOLATILE_BOUNDARY: content below changes per spawn -->")

        # --- Variable suffix ---
        if self.security_block:
            parts.append(_SECURITY_BLOCK)

        worktree_block = self._build_worktree_block()
        if worktree_block:
            parts.append(worktree_block)

        if self.prior_test_runs_block:
            parts.append(self.prior_test_runs_block)

        # Inject previous-attempt context when retries have been recorded.
        prev_context = _build_previous_attempt_context(self.discussion)
        if prev_context:
            parts.append(prev_context)

        if self.task_prompt:
            parts.append(self.task_prompt)

        if self.gate_line:
            parts.append(self.gate_line)

        if self.dial_state_at_spawn:
            parts.append(f"Dial state at spawn: {self.dial_state_at_spawn}")

        if self.hook_event_id:
            parts.append(f"hook_event_id={self.hook_event_id}")

        if self.prompt_manifest:
            parts.append(f"prompt_manifest={json.dumps(self.prompt_manifest)}")

        # Join with double newline between sections
        first = True
        buf: list[str] = []
        for part in parts:
            if first:
                buf.append(part)
                first = False
            else:
                buf.append("\n\n" + part)

        return "".join(buf) + "\n"


# ---------------------------------------------------------------------------
# PSC → SpawnPrompt factory
# ---------------------------------------------------------------------------


def build_from_psc(
    role: str,
    psc_json: dict,
    *,
    discussion: int | None = None,
    task_prompt: str = "",
    hook_event_id: str = "",
    worktree_path: str | None = None,
    worktree_unprovisioned: bool = False,
    worktree_unprovisioned_reason: str = "",
    security_block: bool = False,
    env_scrub_snippet: str = "",
    prior_test_runs_block: str = "",
    prompt_manifest: dict | None = None,
) -> SpawnPrompt:
    """Construct a SpawnPrompt from a pre-spawn-check JSON payload.

    Parameters
    ----------
    role:
        Agent role name (e.g. "executor").
    psc_json:
        Parsed JSON dict from pre-spawn-check.sh output.
    discussion, task_prompt, hook_event_id, worktree_path, worktree_unprovisioned,
    worktree_unprovisioned_reason, security_block, env_scrub_snippet,
    prior_test_runs_block, prompt_manifest:
        Caller-supplied values not present in psc_json.
    """
    persona_voice = psc_json.get("persona_voice", "")
    working_principles = psc_json.get("working_principles", "")
    self_observe_gate = psc_json.get("self_observe_gate", "")

    gates = psc_json.get("gate_context", {}).get("gates", {})
    if gates:
        pairs = ", ".join(f"{k}={v}" for k, v in gates.items())
        gate_line = f"[Control plane gates: {pairs}]"
    else:
        gate_line = ""

    return SpawnPrompt(
        role=role,
        discussion=discussion,
        task_prompt=task_prompt,
        persona_voice=persona_voice,
        working_principles=working_principles,
        self_observe_gate=self_observe_gate,
        gate_line=gate_line,
        worktree_path=worktree_path,
        worktree_unprovisioned=worktree_unprovisioned,
        worktree_unprovisioned_reason=worktree_unprovisioned_reason,
        security_block=security_block,
        hook_event_id=hook_event_id,
        env_scrub_snippet=env_scrub_snippet,
        prior_test_runs_block=prior_test_runs_block,
        prompt_manifest=prompt_manifest or {},
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main_render(argv: list[str]) -> int:
    """CLI: read JSON from stdin (or $SPAWN_PROMPT_JSON), write prompt to stdout."""
    # Prefer env var over stdin to avoid heredoc/pipe quoting issues
    raw = ""
    env_json = __import__("os").environ.get("SPAWN_PROMPT_JSON", "")
    if env_json:
        raw = env_json
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("prompt_builder: empty input — expected JSON on stdin or $SPAWN_PROMPT_JSON", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"prompt_builder: JSONDecodeError: {exc}", file=sys.stderr)
        return 1

    role = data.get("role", "")
    if not role:
        print("prompt_builder: 'role' is required", file=sys.stderr)
        return 1

    # Extract PSC fields if present
    psc_fields = {
        "persona_voice": data.get("persona_voice", ""),
        "working_principles": data.get("working_principles", ""),
        "self_observe_gate": data.get("self_observe_gate", ""),
    }
    gates = data.get("gates", {})
    if gates:
        pairs = ", ".join(f"{k}={v}" for k, v in gates.items())
        gate_line = f"[Control plane gates: {pairs}]"
    else:
        gate_line = data.get("gate_line", "")

    discussion_raw = data.get("discussion")
    discussion = int(discussion_raw) if discussion_raw is not None else None

    worktree_path = data.get("worktree_path") or None
    worktree_unprovisioned = bool(data.get("worktree_unprovisioned", False))
    worktree_unprovisioned_reason = data.get("worktree_unprovisioned_reason", "")
    security_block = bool(data.get("security_block", False))
    hook_event_id = data.get("hook_event_id", "")
    task_prompt = data.get("task_prompt", "")
    env_scrub_snippet = data.get("env_scrub_snippet", "")
    prior_test_runs_block = data.get("prior_test_runs_block", "")
    prompt_manifest = data.get("prompt_manifest", {})
    dial_state_at_spawn = data.get("dial_state_at_spawn", "")
    pr_raw = data.get("pr")
    pr = int(pr_raw) if pr_raw is not None else None
    pr_branch = data.get("pr_branch", "")

    sp = SpawnPrompt(
        role=role,
        discussion=discussion,
        task_prompt=task_prompt,
        persona_voice=psc_fields["persona_voice"],
        working_principles=psc_fields["working_principles"],
        self_observe_gate=psc_fields["self_observe_gate"],
        gate_line=gate_line,
        worktree_path=worktree_path,
        worktree_unprovisioned=worktree_unprovisioned,
        worktree_unprovisioned_reason=worktree_unprovisioned_reason,
        security_block=security_block,
        hook_event_id=hook_event_id,
        env_scrub_snippet=env_scrub_snippet,
        prior_test_runs_block=prior_test_runs_block,
        prompt_manifest=prompt_manifest,
        dial_state_at_spawn=dial_state_at_spawn,
        pr=pr,
        pr_branch=pr_branch,
    )

    # D#1788: a contract violation (a template references a variable with no
    # supplier and no excuse — e.g. {{pr_number}} for a PR-scoped role spawned
    # without --pr) must reach the operator on stderr with a non-zero exit,
    # not render an empty prompt. This is the "un-swallow" half of the fix —
    # the old code let any exception in the render path disappear.
    try:
        rendered = sp.render()
    except Exception as exc:
        print(f"prompt_builder: render failed for role '{role}': {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "render":
        return _main_render(argv[1:])
    print(f"prompt_builder: unknown subcommand: {argv[0] if argv else '(none)'}", file=sys.stderr)
    print("Usage: python3 -m backend.prompt_builder render < input.json", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
