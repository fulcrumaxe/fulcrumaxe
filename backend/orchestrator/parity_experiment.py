"""backend/orchestrator/parity_experiment.py — Per-role SDK-vs-CC parity experiment harness.

Extends parity_harness.py with real dual-run capability: the SDK side uses
ClaudeAgentSDKRunner (subscription login), and the CC side runs an actual
``claude -p`` subprocess (headless Claude Code) with matching args.

DEFAULT-SAFE: No real calls unless BOTH conditions are met:
  1. RUN_SDK_PARITY=1 is set in the environment
  2. detect_sdk_credential() returns "oauth_token" or "login" (subscription auth)
     — API-key-only auth ("api_key") is NOT accepted here because the CC side
       always uses subscription auth; comparing SDK-api-key vs CC-subscription
       would conflate two billing modes, making the cost estimate meaningless.

Without the opt-in, run_role_parity() raises ParityLiveGuardError.

Shadow-only note for gated roles:
  For executor, code-reviewer, security-reviewer, and acceptance-tester the SDK
  output is EXPERIMENT-ONLY — it is never used as the authoritative verdict.
  The CC path (spawn-agent.sh / Agent()) remains the production path. This harness
  is purely a measurement tool so the Team Lead can evaluate whether SDK routing
  would produce equivalent outcomes for those roles.

CC-side invocation approach:
  subprocess.run(["claude", "-p", task_prompt, "--allowedTools", tool_csv],
                 cwd=worktree_path, capture_output=True, timeout=300)
  A Python subprocess call — not a Bash tool call — so it runs in-process and
  is not intercepted by the sandbox hook that guards executor worktrees.
  Verdict is extracted from stdout via _extract_verdict (same as SDK side).
  Token counts: ``claude -p`` emits a usage JSON on stderr when invoked with
  ``--output-format json`` (Claude Code ≥ 1.x). When present we parse it;
  when absent we record 0 and note it in cc_error. Latency is wall-clock via
  time.monotonic().

Report shape (per-role breakdown in ParityReport.diffs, overall in ParityReport):
  Each ParityDiff carries the role name in spec_label (e.g. "executor"),
  sdk_verdict, cc_verdict, verdict_agree (== verdict_match), token_input_delta,
  token_output_delta, sdk_tool_calls, cc_tool_calls, output_similarity.
  run_experiment also returns a list[dict] "per_role" summary alongside the
  ParityReport for easy JSON serialisation.

Usage::

    # Dry mode (no real calls) — smoke test the harness
    python3 backend/orchestrator/parity_experiment.py --dry --roles executor,code-reviewer

    # Live mode (opt-in)
    RUN_SDK_PARITY=1 python3 backend/orchestrator/parity_experiment.py \\
        --roles executor,code-reviewer --json

    # As a library
    from backend.orchestrator.parity_experiment import run_experiment
    from backend.orchestrator.sdk_runner import SpawnSpec
    specs = [SpawnSpec(role="executor", task_prompt="...", tool_whitelist=["Read"])]
    report, per_role = run_experiment(specs)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Bootstrap sys.path so this module can be run directly:
#   python3 backend/orchestrator/parity_experiment.py --dry --roles executor
# and also imported in tests via sys.path.insert in conftest / test files.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import fcntl

# Re-use primitives — do NOT reimplement
from backend.orchestrator.parity_harness import (
    ParityDiff,
    ParityLiveGuardError,
    ParityReport,
    compare_run,
    parity_report,
)
from backend.orchestrator.sdk_runner import RunResult, SpawnSpec, _extract_verdict
from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner, detect_sdk_credential
from backend import state_paths as _state_paths

# ---------------------------------------------------------------------------
# PARITY_HISTORY — resolved at call time (D#1810), with a compatibility shim
# ---------------------------------------------------------------------------
# This used to be `from backend.state_paths import PARITY_HISTORY` at module
# scope, which froze it at import time. Module __getattr__ (PEP 562) makes
# external access (`parity_experiment.PARITY_HISTORY`) resolve fresh on every
# read, UNLESS a caller — several tests do this — assigns/patches the name
# directly (`patch("backend.orchestrator.parity_experiment.PARITY_HISTORY",
# ...)`), which shadows __getattr__ exactly like any other module attribute.
# `_attr()` routes this module's own internal reference through the same
# globals-first-else-resolve-fresh logic so both call sites see one
# consistent value.


def __getattr__(name: str):
    if name == "PARITY_HISTORY":
        return _state_paths.PARITY_HISTORY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _attr(name: str):
    if name in globals():
        return globals()[name]
    return __getattr__(name)


# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------


def write_parity_history(report: "ExperimentReport") -> None:
    """Append one JSON line to the parity-history JSONL file.

    Each line records the UTC timestamp, overall parity stats, and a per-role
    token-delta + verdict summary. The file is created if absent; writes are
    atomic (fcntl.flock) so concurrent callers don't interleave partial lines.

    Parameters
    ----------
    report:
        The ExperimentReport returned by run_experiment().
    """
    ts = datetime.now(timezone.utc).isoformat()
    overall = report.parity.to_dict()
    per_role_summary = [
        {
            "role": entry["role"],
            "sdk_verdict": entry["sdk_verdict"],
            "cc_verdict": entry["cc_verdict"],
            "verdict_agree": entry["verdict_agree"],
            "token_input_delta": entry["token_input_delta"],
            "token_output_delta": entry["token_output_delta"],
            "output_similarity": entry["output_similarity"],
        }
        for entry in report.per_role
    ]
    record = {
        "ts": ts,
        "overall": overall,
        "per_role": per_role_summary,
        "verdict_parse_rate": round(report.verdict_parse_rate, 4),
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"

    history_path: Path = _attr("PARITY_HISTORY")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Live guard
# ---------------------------------------------------------------------------


def _check_experiment_opt_in() -> None:
    """Raise ParityLiveGuardError unless both experiment opt-in conditions are satisfied.

    Conditions (both required):
      1. RUN_SDK_PARITY=1 in environment
      2. detect_sdk_credential() returns "oauth_token" or "login"
         (subscription auth — NOT "api_key", which would mix billing modes)
    """
    if os.environ.get("RUN_SDK_PARITY") != "1":
        raise ParityLiveGuardError(
            "Live parity experiment blocked: RUN_SDK_PARITY=1 is not set.\n"
            "Set RUN_SDK_PARITY=1 to enable live dual-run comparisons."
        )
    cred = detect_sdk_credential()
    if cred not in ("oauth_token", "login"):
        raise ParityLiveGuardError(
            f"Live parity experiment blocked: detect_sdk_credential() returned {cred!r}.\n"
            "Subscription login ('oauth_token' or 'login') is required for the CC side.\n"
            "An API key alone ('api_key') cannot be used: it would compare different billing modes."
        )


# ---------------------------------------------------------------------------
# CC-side runner
# ---------------------------------------------------------------------------


def _parse_cc_stream_json(stdout: str) -> tuple[str, int, int, int]:
    """Parse ``--output-format stream-json`` JSONL output from ``claude -p``.

    Returns
    -------
    (final_text, input_tokens, output_tokens, tool_calls_count)

    The stream-json format emits one JSON object per line.  Relevant event
    shapes (Claude Code >= 1.x):

    * ``{"type": "assistant", "message": {"content": [...], ...}}``
      Content blocks may include ``{"type": "tool_use", ...}`` entries — each
      one represents a real tool call.  We count these to get ``cc_tool_calls``.
      Text blocks (``{"type": "text", "text": "..."}```) contribute to
      ``final_text``.

    * ``{"type": "result", "subtype": "success", "result": "...",
          "usage": {"input_tokens": N, "output_tokens": M}}``
      The terminal summary line.  ``result`` is the final assistant text
      (authoritative); ``usage`` carries the token counts.

    We prefer the ``result`` field from the terminal summary for
    ``final_text``; text blocks from assistant events are a fallback.
    """
    input_tokens = 0
    output_tokens = 0
    tool_calls_count = 0
    final_text = ""
    text_parts: list[str] = []

    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")

        if event_type == "assistant":
            # Count tool_use blocks and collect text
            message = event.get("message") or {}
            content = message.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_calls_count += 1
                    elif block.get("type") == "text":
                        txt = block.get("text", "")
                        if txt:
                            text_parts.append(txt)

        elif event_type == "result":
            # Terminal summary — authoritative for text + usage
            result_val = event.get("result", "")
            if result_val:
                final_text = result_val
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens = int(usage.get("input_tokens", 0) or 0)
                output_tokens = int(usage.get("output_tokens", 0) or 0)

    # Fallback: join text from assistant events if result line was absent/empty
    if not final_text and text_parts:
        final_text = "\n".join(text_parts)

    return final_text, input_tokens, output_tokens, tool_calls_count


def _run_cc_side(spec: SpawnSpec) -> RunResult:
    """Invoke ``claude -p`` as a subprocess and return a RunResult.

    The CC side runs headless Claude Code with:
      - ``--print`` / ``-p`` flag for non-interactive mode
      - ``--allowedTools`` set to spec.tool_whitelist (comma-joined)
      - ``--output-format stream-json`` so each event (including tool_use
        blocks) is emitted as a separate JSONL line — this is the only way
        to count real tool calls from the CC side.  The previous
        ``--output-format json`` mode only emits a final summary with no
        per-turn breakdown, so ``tool_calls_count`` was always 0.
      - cwd = spec.worktree_path (or repo root as fallback)
      - timeout = 300 s

    Token counts and tool-call counts are extracted by _parse_cc_stream_json.

    This is a Python subprocess call — not a Bash tool call — so it runs
    in-process and is not intercepted by the executor sandbox hook.
    """
    agent_id = f"cc-live-{spec.role}-{int(time.time())}"
    cwd = spec.worktree_path or str(Path.cwd())

    cmd = ["claude", "-p", spec.task_prompt]
    if spec.tool_whitelist:
        cmd += ["--allowedTools", ",".join(spec.tool_whitelist)]
    cmd += ["--output-format", "stream-json"]

    start_wall = time.monotonic()
    start_ts = datetime.now(timezone.utc).isoformat()

    input_tokens = 0
    output_tokens = 0
    tool_calls_count = 0
    final_text = ""
    error: Optional[str] = None
    verdict = "unknown"

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Parse the stream-json JSONL for text, tokens, and tool-call count
        final_text, input_tokens, output_tokens, tool_calls_count = (
            _parse_cc_stream_json(stdout)
        )

        # Fallback: raw stdout as final_text when stream parsing found nothing
        if not final_text:
            final_text = stdout.strip()

        if proc.returncode != 0 and not final_text:
            error = f"claude -p exited {proc.returncode}: {stderr[:400]}"
            verdict = "fail"
        else:
            verdict = _extract_verdict(final_text)
            if proc.returncode != 0:
                error = f"claude -p exited {proc.returncode} (output captured)"

    except subprocess.TimeoutExpired:
        error = "claude -p timed out after 300s"
        verdict = "fail"
        final_text = ""
    except FileNotFoundError:
        error = "claude binary not found — is claude CLI installed?"
        verdict = "fail"
        final_text = ""
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        verdict = "fail"
        final_text = ""

    end_ts = datetime.now(timezone.utc).isoformat()

    return RunResult(
        agent_id=agent_id,
        role=spec.role,
        discussion=spec.discussion,
        pr=spec.pr,
        verdict=verdict,
        final_text=final_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_calls_count=tool_calls_count,
        prompt_sha256="",
        start_ts=start_ts,
        end_ts=end_ts,
        error=error,
        routed_via="cc",
    )


# ---------------------------------------------------------------------------
# Per-role experiment
# ---------------------------------------------------------------------------


def run_role_parity(spec: SpawnSpec) -> ParityDiff:
    """Run a real dual comparison for one SpawnSpec and return a ParityDiff.

    SDK side: ClaudeAgentSDKRunner().run(spec) (subscription login).
    CC side:  _run_cc_side(spec) — ``claude -p`` subprocess with matching args.

    Both results are passed to compare_run() which computes the ParityDiff.
    The diff is tagged with spec.role as the spec_label.

    Raises
    ------
    ParityLiveGuardError
        When RUN_SDK_PARITY=1 is not set or no subscription credential exists.
        Call with the env var set only after confirming this is intentional — it
        will make real API/subscription calls.

    Notes
    -----
    For executor, code-reviewer, security-reviewer, acceptance-tester:
      The SDK result is SHADOW-ONLY — never the authoritative verdict.
      spawn-agent.sh / Agent() remains the production path for those roles.
    """
    import asyncio

    _check_experiment_opt_in()

    sdk_runner = ClaudeAgentSDKRunner()
    sdk_result = asyncio.run(sdk_runner.run(spec))
    cc_result = _run_cc_side(spec)

    return compare_run(sdk_result, cc_result, spec_label=spec.role)


# ---------------------------------------------------------------------------
# Multi-role experiment
# ---------------------------------------------------------------------------


@dataclass
class ExperimentReport:
    """Full parity experiment report: overall + per-role breakdown.

    verdict_parse_rate: fraction of runs (across both SDK and CC sides) where
    the verdict was successfully parsed from an AGENT_OUTPUT envelope
    (i.e. verdict != "unknown" and verdict != a dry-stub label).
    A value >= 0.95 indicates the task prompts are representative and
    verdict-emitting.  A low value means agents aren't producing
    parseable envelopes — the measurement is hollow.
    """

    parity: ParityReport
    per_role: list[dict]
    verdict_parse_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "overall": self.parity.to_dict(),
            "per_role": self.per_role,
            "verdict_parse_rate": round(self.verdict_parse_rate, 4),
        }


def _compute_verdict_parse_rate(per_role: list[dict]) -> float:
    """Return fraction of verdict slots that produced a parseable verdict.

    Each per_role entry contributes two verdict slots: sdk_verdict and
    cc_verdict.  A slot is considered "parsed" when its verdict is not
    "unknown" and does not contain the string "dry_stub".
    """
    if not per_role:
        return 0.0
    _unparseable = {"unknown"}
    total_slots = len(per_role) * 2
    parsed = sum(
        (1 if entry["sdk_verdict"] not in _unparseable and "dry_stub" not in entry["sdk_verdict"] else 0)
        + (1 if entry["cc_verdict"] not in _unparseable and "dry_stub" not in entry["cc_verdict"] else 0)
        for entry in per_role
    )
    return parsed / total_slots if total_slots > 0 else 0.0


def run_experiment(specs: list[SpawnSpec]) -> ExperimentReport:
    """Run run_role_parity for each spec, aggregate, and return an ExperimentReport.

    Calls _check_experiment_opt_in() once at the top — all real calls are
    guarded by that single check.

    Parameters
    ----------
    specs:
        List of SpawnSpec objects. Each should have a distinct role field so the
        per-role breakdown is meaningful. Multiple specs for the same role are
        allowed and will each appear as separate per_role entries.

    Returns
    -------
    ExperimentReport
        .parity  — overall ParityReport (aggregated stats across all specs)
        .per_role — list[dict] with one entry per spec; keys:
                    role, spec_label, sdk_verdict, cc_verdict, verdict_agree,
                    token_input_delta, token_output_delta, sdk_tool_calls,
                    cc_tool_calls, output_similarity, sdk_error, cc_error
        .verdict_parse_rate — fraction of verdict slots that produced a
                    parseable (non-"unknown") verdict; >= 0.95 is the target
    """
    _check_experiment_opt_in()

    diffs: list[ParityDiff] = []
    per_role: list[dict] = []

    for spec in specs:
        import asyncio

        # Run SDK side
        sdk_runner = ClaudeAgentSDKRunner()
        sdk_result = asyncio.run(sdk_runner.run(spec))

        # Run CC side
        cc_result = _run_cc_side(spec)

        diff = compare_run(sdk_result, cc_result, spec_label=spec.role)
        diffs.append(diff)

        per_role.append({
            "role": spec.role,
            "spec_label": diff.spec_label,
            "sdk_verdict": diff.sdk_verdict,
            "cc_verdict": diff.cc_verdict,
            "verdict_agree": diff.verdict_match,
            "token_input_delta": diff.token_input_delta,
            "token_output_delta": diff.token_output_delta,
            "sdk_tool_calls": diff.sdk_tool_calls,
            "cc_tool_calls": diff.cc_tool_calls,
            "output_similarity": round(diff.output_similarity, 4),
            "sdk_error": diff.sdk_error,
            "cc_error": diff.cc_error,
        })

    parse_rate = _compute_verdict_parse_rate(per_role)
    experiment_report = ExperimentReport(
        parity=parity_report(diffs),
        per_role=per_role,
        verdict_parse_rate=parse_rate,
    )
    write_parity_history(experiment_report)
    return experiment_report


# ---------------------------------------------------------------------------
# Default role experiment specs
# ---------------------------------------------------------------------------

# AGENT_OUTPUT envelope format injected into every task prompt so both SDK
# and CC sides produce parseable verdicts.  This is the literal format
# _extract_verdict expects.
_AGENT_OUTPUT_INSTRUCTIONS = """

After completing the task, you MUST end your response with an AGENT_OUTPUT
envelope in EXACTLY this format (copy the markers verbatim):

<!-- AGENT_OUTPUT -->
```json
{
  "agent": "ROLE_NAME",
  "verdict": "VERDICT_VALUE",
  "files_touched": []
}
```
<!-- /AGENT_OUTPUT -->

Replace ROLE_NAME with your role and VERDICT_VALUE with the appropriate
verdict string (done, pass, fail, needs-fix, skip) as instructed above.
Do not omit the envelope — it is required for measurement.
"""

# Representative SpawnSpec per role for parity measurement runs.
#
# Design constraints:
#   1. Each task requires at least one real tool call (Read or Bash) so
#      cc_tool_calls is reliably > 0 on a functioning CC run.
#   2. Each prompt includes _AGENT_OUTPUT_INSTRUCTIONS so both SDK and CC
#      sides produce a parseable AGENT_OUTPUT envelope → verdict != unknown.
#   3. Tasks are bounded and safe — no writes, no network, no spawns.
#   4. Tasks are representative of each role's real work, not trivial no-ops.
ROLE_EXPERIMENT_SPECS: dict[str, SpawnSpec] = {
    "executor": SpawnSpec(
        role="executor",
        task_prompt=(
            "You are an executor agent. Read the file "
            "backend/orchestrator/parity_harness.py and report: (a) the names "
            "of all top-level functions it defines, and (b) the approximate "
            "line count. Use the Read tool to fetch the file."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "executor")
            .replace("VERDICT_VALUE", "done")
        ),
        tool_whitelist=["Read", "Bash"],
        isolation="worktree",
        sdk_eligible=False,  # gated — shadow-only
    ),
    "code-reviewer": SpawnSpec(
        role="code-reviewer",
        task_prompt=(
            "You are a code-reviewer agent. Read "
            "backend/orchestrator/parity_harness.py and check: "
            "(1) every public function has a docstring, "
            "(2) no bare except clauses are present. "
            "Use the Read tool to inspect the file. "
            "Verdict: 'pass' if both checks pass, 'needs-fix' if either fails."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "code-reviewer")
            .replace("VERDICT_VALUE", "pass or needs-fix")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=False,  # gated — shadow-only
    ),
    "security-reviewer": SpawnSpec(
        role="security-reviewer",
        task_prompt=(
            "You are a security-reviewer agent. Read "
            "backend/orchestrator/sdk_runner.py and verify: "
            "(1) ANTHROPIC_API_KEY is never written to logs or audit trails, "
            "(2) _write_audit calls redact() before writing. "
            "Use the Read tool to inspect the file. "
            "Verdict: 'pass' if no leaks found, 'needs-fix' if any leak exists."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "security-reviewer")
            .replace("VERDICT_VALUE", "pass or needs-fix")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=False,  # gated — shadow-only
    ),
    "acceptance-tester": SpawnSpec(
        role="acceptance-tester",
        task_prompt=(
            "You are an acceptance-tester agent. Read "
            "backend/orchestrator/parity_harness.py and verify the following "
            "acceptance criteria: "
            "(AC1) compare_run function is exported, "
            "(AC2) parity_report function is exported, "
            "(AC3) ParityDiff dataclass is defined with a verdict_match field. "
            "Use the Read tool to inspect the file. "
            "Verdict: 'pass' if all three ACs pass, 'fail' if any are missing."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "acceptance-tester")
            .replace("VERDICT_VALUE", "pass or fail")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=False,  # gated — shadow-only
    ),
    "docs-writer": SpawnSpec(
        role="docs-writer",
        task_prompt=(
            "You are a docs-writer agent. Read "
            "backend/orchestrator/parity_harness.py using the Read tool, then "
            "write a concise plain-English summary (2-4 sentences) of what the "
            "module does, who its consumers are, and what parity_report returns."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "docs-writer")
            .replace("VERDICT_VALUE", "done")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=True,
    ),
    "run-analyst": SpawnSpec(
        role="run-analyst",
        task_prompt=(
            "You are a run-analyst agent. Use the Read tool to read "
            "backend/orchestrator/parity_harness.py and report: "
            "(a) how many dataclasses are defined, "
            "(b) how many functions/methods are defined total, "
            "(c) whether parity_report handles the empty-list case explicitly."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "run-analyst")
            .replace("VERDICT_VALUE", "done")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=True,
    ),
    "quality-sweep": SpawnSpec(
        role="quality-sweep",
        task_prompt=(
            "You are a quality-sweep agent. Read "
            "backend/orchestrator/parity_harness.py using the Read tool and "
            "check for: (a) functions lacking docstrings, (b) TODO/FIXME "
            "comments, (c) bare except clauses. Report all findings. "
            "Verdict: 'pass' if no issues found, 'needs-fix' if any exist."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "quality-sweep")
            .replace("VERDICT_VALUE", "pass or needs-fix")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=True,
    ),
    "feedback-scanner": SpawnSpec(
        role="feedback-scanner",
        task_prompt=(
            "You are a feedback-scanner agent. Read "
            "backend/orchestrator/parity_harness.py using the Read tool and "
            "identify any improvement opportunities: missing edge-case handling, "
            "unclear variable names, or missing type annotations. "
            "List each finding with a brief explanation."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "feedback-scanner")
            .replace("VERDICT_VALUE", "done")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=True,
    ),
    "mission-analyst": SpawnSpec(
        role="mission-analyst",
        task_prompt=(
            "You are a mission-analyst agent. Read the file README.md using "
            "the Read tool and describe in 1-2 sentences: (a) the primary "
            "mission of this repository, (b) the main deliverable it produces."
            + _AGENT_OUTPUT_INSTRUCTIONS.replace("ROLE_NAME", "mission-analyst")
            .replace("VERDICT_VALUE", "done")
        ),
        tool_whitelist=["Read"],
        isolation="worktree",
        sdk_eligible=True,
    ),
}


# ---------------------------------------------------------------------------
# Dry-mode helpers (mocked results, no real calls)
# ---------------------------------------------------------------------------


def _make_dry_run_result(spec: SpawnSpec, side: str) -> RunResult:
    """Return a stub RunResult for dry-mode report building.

    Parameters
    ----------
    spec:  the SpawnSpec
    side:  "sdk" or "cc"
    """
    return RunResult(
        agent_id=f"{side}-dry-{spec.role}",
        role=spec.role,
        discussion=spec.discussion,
        pr=spec.pr,
        verdict=f"{side}_dry_stub",
        final_text=f"[{side.upper()} dry stub — no real call made]",
        input_tokens=0,
        output_tokens=0,
        tool_calls_count=0,
        prompt_sha256="",
        start_ts="",
        end_ts="",
        error=None,
        routed_via=side,
    )


def run_experiment_dry(specs: list[SpawnSpec]) -> ExperimentReport:
    """Build an ExperimentReport from mocked RunResults — no real calls.

    Used for smoke-testing the report shape and CLI output without
    spending any tokens.
    """
    diffs: list[ParityDiff] = []
    per_role: list[dict] = []

    for spec in specs:
        sdk_result = _make_dry_run_result(spec, "sdk")
        cc_result = _make_dry_run_result(spec, "cc")
        diff = compare_run(sdk_result, cc_result, spec_label=spec.role)
        diffs.append(diff)

        per_role.append({
            "role": spec.role,
            "spec_label": diff.spec_label,
            "sdk_verdict": diff.sdk_verdict,
            "cc_verdict": diff.cc_verdict,
            "verdict_agree": diff.verdict_match,
            "token_input_delta": diff.token_input_delta,
            "token_output_delta": diff.token_output_delta,
            "sdk_tool_calls": diff.sdk_tool_calls,
            "cc_tool_calls": diff.cc_tool_calls,
            "output_similarity": round(diff.output_similarity, 4),
            "sdk_error": diff.sdk_error,
            "cc_error": diff.cc_error,
        })

    parse_rate = _compute_verdict_parse_rate(per_role)
    return ExperimentReport(
        parity=parity_report(diffs),
        per_role=per_role,
        verdict_parse_rate=parse_rate,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Per-role SDK-vs-CC parity experiment harness.\n"
            "Default mode: --dry (no real calls, report shape only).\n"
            "Live mode: requires RUN_SDK_PARITY=1 + subscription login."
        )
    )
    parser.add_argument(
        "--roles",
        required=True,
        help=(
            "Comma-separated role names to include, e.g. "
            "'executor,code-reviewer'. Use 'all' to run all default roles."
        ),
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        default=False,
        help="Dry mode: build report from mocked RunResults (no real calls).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json_output",
        help="Output report as JSON (default: human-readable summary).",
    )
    args = parser.parse_args()

    # Resolve roles
    if args.roles.strip().lower() == "all":
        role_names = list(ROLE_EXPERIMENT_SPECS.keys())
    else:
        role_names = [r.strip() for r in args.roles.split(",") if r.strip()]

    unknown = [r for r in role_names if r not in ROLE_EXPERIMENT_SPECS]
    if unknown:
        print(
            f"[parity_experiment] WARNING: unknown roles (no default spec): {unknown}",
            file=sys.stderr,
        )

    specs = [ROLE_EXPERIMENT_SPECS[r] for r in role_names if r in ROLE_EXPERIMENT_SPECS]
    if not specs:
        print("[parity_experiment] ERROR: no valid roles found.", file=sys.stderr)
        sys.exit(1)

    if args.dry:
        print(
            "[parity_experiment] DRY mode — no real calls made. "
            "Use without --dry (and with RUN_SDK_PARITY=1) for live runs.",
            file=sys.stderr,
        )
        report = run_experiment_dry(specs)
    else:
        # Live mode — guard first
        try:
            _check_experiment_opt_in()
        except ParityLiveGuardError as exc:
            print(f"[parity_experiment] BLOCKED: {exc}", file=sys.stderr)
            sys.exit(1)

        print(
            f"[parity_experiment] LIVE mode — running dual comparison for {len(specs)} role(s).",
            file=sys.stderr,
        )
        report = run_experiment(specs)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        # Human-readable summary
        p = report.parity
        print(f"\n=== Parity Experiment Report ({len(specs)} role(s)) ===")
        print(f"  Verdict match rate : {p.verdict_match_rate:.0%} ({p.verdict_match_count}/{p.total_specs})")
        print(f"  Verdict parse rate : {report.verdict_parse_rate:.0%}  (target >= 95%)")
        print(f"  Avg input delta    : {p.avg_token_input_delta:+.1f} tokens (sdk - cc)")
        print(f"  Avg output delta   : {p.avg_token_output_delta:+.1f} tokens (sdk - cc)")
        print(f"  Avg output sim     : {p.avg_output_similarity:.2%}")
        print()
        print(f"  {'Role':<22} {'SDK verdict':<16} {'CC verdict':<16} {'Match':<8} {'Sim':<8} {'CC tools'}")
        print(f"  {'-'*22} {'-'*16} {'-'*16} {'-'*8} {'-'*8} {'-'*8}")
        for pr_entry in report.per_role:
            match_str = "yes" if pr_entry["verdict_agree"] else "NO"
            print(
                f"  {pr_entry['role']:<22} "
                f"{pr_entry['sdk_verdict']:<16} "
                f"{pr_entry['cc_verdict']:<16} "
                f"{match_str:<8} "
                f"{pr_entry['output_similarity']:.2%}   "
                f"{pr_entry['cc_tool_calls']}"
            )
        print()


if __name__ == "__main__":
    _cli_main()
