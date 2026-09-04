"""backend/orchestrator/parity_harness.py — SDK-vs-CC parity test harness (Phase 3).

Compares outcomes of running the same spawn spec through both the SDK path
(SDKRunner) and the Claude Code path, producing a structured diff report.

DEFAULT-SAFE: Live SDK invocations are OPT-IN ONLY.
  The harness never makes a real Anthropic API call unless BOTH:
    1. Environment variable RUN_SDK_PARITY=1 is set, AND
    2. A real ANTHROPIC_API_KEY is present (in keychain or credentials file).
  Without those, compare_run() raises ParityLiveGuardError.

Usage as a library::

    from backend.orchestrator.parity_harness import compare_run, parity_report
    from backend.orchestrator.sdk_runner import SpawnSpec, RunResult

    # Build mock/fixture results for offline testing
    sdk_result = RunResult(...)
    cc_result = RunResult(...)

    diff = compare_run(sdk_result, cc_result)
    report = parity_report([diff])

Usage as a CLI (dry mode — no real SDK calls)::

    python3 backend/orchestrator/parity_harness.py --discussions 101,102

Usage as a CLI (live mode — REQUIRES RUN_SDK_PARITY=1 + ANTHROPIC_API_KEY)::

    RUN_SDK_PARITY=1 python3 backend/orchestrator/parity_harness.py --discussions 101,102 --live
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Public guard exception
# ---------------------------------------------------------------------------


class ParityLiveGuardError(RuntimeError):
    """Raised when live SDK invocation is attempted without explicit opt-in.

    Two conditions must both be true for live calls to proceed:
      - RUN_SDK_PARITY=1 is set in the environment
      - A real ANTHROPIC_API_KEY is available (keychain or credentials file)
    """


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ParityDiff:
    """Structured comparison of SDK vs CC run results for one spec."""

    spec_label: str  # human-readable identifier (e.g. "discussion-101")
    sdk_verdict: str
    cc_verdict: str
    verdict_match: bool
    sdk_input_tokens: int
    cc_input_tokens: int
    token_input_delta: int  # sdk - cc (positive = SDK used more)
    sdk_output_tokens: int
    cc_output_tokens: int
    token_output_delta: int  # sdk - cc
    sdk_tool_calls: int
    cc_tool_calls: int
    tool_call_delta: int  # sdk - cc
    # 0.0–1.0 similarity of final_text (normalized Jaccard on word sets)
    output_similarity: float
    sdk_error: Optional[str] = None
    cc_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "spec_label": self.spec_label,
            "sdk_verdict": self.sdk_verdict,
            "cc_verdict": self.cc_verdict,
            "verdict_match": self.verdict_match,
            "token_input_delta": self.token_input_delta,
            "token_output_delta": self.token_output_delta,
            "tool_call_delta": self.tool_call_delta,
            "output_similarity": round(self.output_similarity, 4),
            "sdk_error": self.sdk_error,
            "cc_error": self.cc_error,
        }


@dataclass
class ParityReport:
    """Aggregated parity report across multiple compare_run() results."""

    total_specs: int
    verdict_match_count: int
    verdict_mismatch_count: int
    avg_token_input_delta: float
    avg_token_output_delta: float
    avg_tool_call_delta: float
    avg_output_similarity: float
    diffs: list[ParityDiff] = field(default_factory=list)

    @property
    def verdict_match_rate(self) -> float:
        if self.total_specs == 0:
            return 0.0
        return self.verdict_match_count / self.total_specs

    def to_dict(self) -> dict:
        return {
            "total_specs": self.total_specs,
            "verdict_match_count": self.verdict_match_count,
            "verdict_mismatch_count": self.verdict_mismatch_count,
            "verdict_match_rate": round(self.verdict_match_rate, 4),
            "avg_token_input_delta": round(self.avg_token_input_delta, 2),
            "avg_token_output_delta": round(self.avg_token_output_delta, 2),
            "avg_tool_call_delta": round(self.avg_tool_call_delta, 2),
            "avg_output_similarity": round(self.avg_output_similarity, 4),
            "diffs": [d.to_dict() for d in self.diffs],
        }


# ---------------------------------------------------------------------------
# Similarity helper
# ---------------------------------------------------------------------------


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Normalized word-level Jaccard similarity between two texts.

    Returns 1.0 when both texts are identical, 0.0 when they share no words.
    Empty-string pair returns 1.0 (both vacuously identical).
    """
    if not text_a and not text_b:
        return 1.0
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a and not words_b:
        return 1.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Core comparison function
# ---------------------------------------------------------------------------


def compare_run(
    sdk_result: "RunResult",  # noqa: F821 — imported at call sites
    cc_result: "RunResult",
    spec_label: str = "",
) -> ParityDiff:
    """Compare two RunResult objects (SDK vs CC) and return a ParityDiff.

    This is a PURE COMPARISON function — it never makes any API calls.
    Both RunResult objects are pre-computed by the caller.

    Parameters
    ----------
    sdk_result:   RunResult from SDKRunner.run()
    cc_result:    RunResult representing the CC path outcome
    spec_label:   Human-readable label (e.g. "discussion-101"); defaults to
                  sdk_result.agent_id if not provided.
    """
    label = spec_label or getattr(sdk_result, "agent_id", "unknown")

    token_input_delta = sdk_result.input_tokens - cc_result.input_tokens
    token_output_delta = sdk_result.output_tokens - cc_result.output_tokens
    tool_call_delta = sdk_result.tool_calls_count - cc_result.tool_calls_count
    verdict_match = sdk_result.verdict == cc_result.verdict
    similarity = _jaccard_similarity(sdk_result.final_text, cc_result.final_text)

    return ParityDiff(
        spec_label=label,
        sdk_verdict=sdk_result.verdict,
        cc_verdict=cc_result.verdict,
        verdict_match=verdict_match,
        sdk_input_tokens=sdk_result.input_tokens,
        cc_input_tokens=cc_result.input_tokens,
        token_input_delta=token_input_delta,
        sdk_output_tokens=sdk_result.output_tokens,
        cc_output_tokens=cc_result.output_tokens,
        token_output_delta=token_output_delta,
        sdk_tool_calls=sdk_result.tool_calls_count,
        cc_tool_calls=cc_result.tool_calls_count,
        tool_call_delta=tool_call_delta,
        output_similarity=similarity,
        sdk_error=sdk_result.error,
        cc_error=cc_result.error,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def parity_report(diffs: list[ParityDiff]) -> ParityReport:
    """Aggregate a list of ParityDiff results into a ParityReport.

    Handles empty list cleanly (returns zero-count report with 0.0 averages).
    """
    total = len(diffs)
    if total == 0:
        return ParityReport(
            total_specs=0,
            verdict_match_count=0,
            verdict_mismatch_count=0,
            avg_token_input_delta=0.0,
            avg_token_output_delta=0.0,
            avg_tool_call_delta=0.0,
            avg_output_similarity=0.0,
            diffs=[],
        )

    match_count = sum(1 for d in diffs if d.verdict_match)
    mismatch_count = total - match_count

    avg_input_delta = sum(d.token_input_delta for d in diffs) / total
    avg_output_delta = sum(d.token_output_delta for d in diffs) / total
    avg_tool_delta = sum(d.tool_call_delta for d in diffs) / total
    avg_similarity = sum(d.output_similarity for d in diffs) / total

    return ParityReport(
        total_specs=total,
        verdict_match_count=match_count,
        verdict_mismatch_count=mismatch_count,
        avg_token_input_delta=avg_input_delta,
        avg_token_output_delta=avg_output_delta,
        avg_tool_call_delta=avg_tool_delta,
        avg_output_similarity=avg_similarity,
        diffs=diffs,
    )


# ---------------------------------------------------------------------------
# Live-run guard + live comparison
# ---------------------------------------------------------------------------


def _check_live_opt_in() -> None:
    """Raise ParityLiveGuardError unless both opt-in conditions are satisfied.

    Conditions (both required):
      1. RUN_SDK_PARITY=1 in environment
      2. ANTHROPIC_API_KEY present in environment (checked here for fast-fail;
         SDKRunner itself loads from keychain/credentials — the env var check
         here is just a proxy to confirm the operator knows what they're doing)
    """
    if os.environ.get("RUN_SDK_PARITY") != "1":
        raise ParityLiveGuardError(
            "Live SDK parity run blocked: RUN_SDK_PARITY=1 is not set.\n"
            "Set RUN_SDK_PARITY=1 to enable live SDK invocations. "
            "This guard prevents accidental real API calls in CI or on merge."
        )
    # Also require ANTHROPIC_API_KEY to be resolvable (fast-fail before spending credits)
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not api_key_present:
        # Try credentials file as a proxy
        creds = Path.home() / ".anthropic" / "credentials"
        api_key_present = creds.exists()
    if not api_key_present:
        raise ParityLiveGuardError(
            "Live SDK parity run blocked: no ANTHROPIC_API_KEY found.\n"
            "Set ANTHROPIC_API_KEY or write ~/.anthropic/credentials before using --live."
        )


def _make_cc_stub_result(spec: "SpawnSpec", discussion: int) -> "RunResult":  # noqa: F821
    """Return a stub RunResult representing the CC path in dry mode.

    The CC path is the existing claude -p / Agent() call — in dry/report mode
    we cannot actually invoke it, so we return a clearly labelled stub.
    """
    from backend.orchestrator.sdk_runner import RunResult
    return RunResult(
        agent_id=f"cc-stub-{discussion}",
        role=spec.role,
        discussion=spec.discussion,
        pr=spec.pr,
        verdict="cc_stub",
        final_text="[CC stub — live path not invoked in dry mode]",
        input_tokens=0,
        output_tokens=0,
        tool_calls_count=0,
        prompt_sha256="",
        start_ts="",
        end_ts="",
        error=None,
    )


async def _live_compare_discussion(discussion_number: int) -> ParityDiff:
    """Run a live SDK spawn for a single Discussion and compare to CC stub.

    The CC path in live mode still uses a stub — a full CC invocation would
    require spawning a Claude Code subprocess, which is out of scope for this
    harness. The SDK-vs-CC parity goal is to compare SDK outcomes against
    known-good CC results from historical agent_run data (a future extension).
    """
    from backend.orchestrator.sdk_runner import SDKRunner, SpawnSpec

    spec = SpawnSpec(
        role="code-reviewer",
        task_prompt=f"Parity test probe for Discussion #{discussion_number}. Return a minimal AGENT_OUTPUT envelope.",
        tool_whitelist=["Read"],
        discussion=discussion_number,
    )

    runner = SDKRunner()  # loads key from keychain/credentials
    sdk_result = await runner.run(spec)
    cc_stub = _make_cc_stub_result(spec, discussion_number)

    return compare_run(sdk_result, cc_stub, spec_label=f"discussion-{discussion_number}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli_main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description=(
            "SDK-vs-CC parity harness. Default mode is DRY (no API calls).\n"
            "Use --live to invoke the SDK path (requires RUN_SDK_PARITY=1 + API key)."
        )
    )
    parser.add_argument(
        "--discussions",
        required=True,
        help="Comma-separated Discussion numbers, e.g. 101,102,103",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Actually invoke the SDK path. "
            "Requires RUN_SDK_PARITY=1 in environment AND a real ANTHROPIC_API_KEY. "
            "Without this flag, produces a dry report-structure with CC stubs."
        ),
    )
    args = parser.parse_args()

    discussion_numbers: list[int] = []
    for part in args.discussions.split(","):
        part = part.strip()
        if part:
            try:
                discussion_numbers.append(int(part))
            except ValueError:
                print(f"[parity_harness] WARNING: skipping non-integer '{part}'", file=sys.stderr)

    if not discussion_numbers:
        print("[parity_harness] ERROR: no valid discussion numbers provided.", file=sys.stderr)
        sys.exit(1)

    if args.live:
        # Guard must pass before any live SDK call
        try:
            _check_live_opt_in()
        except ParityLiveGuardError as e:
            print(f"[parity_harness] BLOCKED: {e}", file=sys.stderr)
            sys.exit(1)

        print(
            f"[parity_harness] LIVE mode — running SDK path for {len(discussion_numbers)} discussion(s).",
            file=sys.stderr,
        )

        async def run_all() -> list[ParityDiff]:
            diffs = []
            for d_num in discussion_numbers:
                print(f"[parity_harness] Running live compare for discussion {d_num}...", file=sys.stderr)
                diff = await _live_compare_discussion(d_num)
                diffs.append(diff)
            return diffs

        import asyncio as _asyncio
        diffs = _asyncio.run(run_all())
    else:
        # Dry mode — build stub results to demonstrate report structure
        from backend.orchestrator.sdk_runner import RunResult, SpawnSpec

        print(
            "[parity_harness] DRY mode — no SDK calls made. "
            "Use --live (with RUN_SDK_PARITY=1 + API key) for real comparisons.",
            file=sys.stderr,
        )
        diffs = []
        for d_num in discussion_numbers:
            spec = SpawnSpec(
                role="code-reviewer",
                task_prompt=f"Parity probe for Discussion #{d_num}",
                tool_whitelist=["Read"],
                discussion=d_num,
            )
            sdk_stub = RunResult(
                agent_id=f"sdk-dry-stub-{d_num}",
                role=spec.role,
                discussion=d_num,
                pr=None,
                verdict="sdk_dry_stub",
                final_text="[SDK dry stub — no live call made]",
                input_tokens=0,
                output_tokens=0,
                tool_calls_count=0,
                prompt_sha256="",
                start_ts="",
                end_ts="",
                error=None,
            )
            cc_stub = _make_cc_stub_result(spec, d_num)
            diff = compare_run(sdk_stub, cc_stub, spec_label=f"discussion-{d_num}")
            diffs.append(diff)

    report = parity_report(diffs)
    print(json.dumps(report.to_dict(), indent=2))


if __name__ == "__main__":
    _cli_main()
