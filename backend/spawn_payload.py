"""backend/spawn_payload.py — build the SPAWN_PROMPT_JSON payload handed to
backend.prompt_builder, from the same env vars scripts/spawn-agent.sh already
sets on every spawn.

Extracted from the inline `python3 -c` heredoc that used to live in
scripts/spawn-agent.sh, wrapped in `2>/dev/null`. That heredoc built a
13-key dict with no `pr` key — `--pr` was parsed, used elsewhere in the
script, and then simply never reached the renderer (D#1788). A missing dict
key inside an unreviewable, stderr-suppressed heredoc is exactly the kind of
bug that goes unnoticed; making the payload shape an importable function
means it is testable and inspectable from a plain command line instead.

Usage (library):
    from backend.spawn_payload import build_payload
    payload = build_payload(os.environ)

Usage (CLI — what scripts/spawn-agent.sh invokes, and how to inspect the
payload by hand):
    _ROLE=code-reviewer _DISC=1761 _PR=1786 PSC_JSON_INPUT='{}' \\
      python3 -m backend.spawn_payload
"""

from __future__ import annotations

import json
import os
import sys
from typing import Mapping


def build_payload(env: Mapping[str, str]) -> dict:
    """Build the dict serialized into SPAWN_PROMPT_JSON for backend.prompt_builder.

    Reads the env var names scripts/spawn-agent.sh already sets when it
    invokes this module. Every var is optional except PSC_JSON_INPUT, which
    defaults to an empty JSON object when absent so this can be probed
    standalone (see module docstring example).
    """
    psc = json.loads(env.get("PSC_JSON_INPUT") or "{}")
    gates = psc.get("gate_context", {}).get("gates", {})
    pairs = ", ".join(f"{k}={v}" for k, v in gates.items()) if gates else ""
    gate_line = f"[Control plane gates: {pairs}]" if pairs else ""

    disc_raw = env.get("_DISC", "")
    pr_raw = env.get("_PR", "")

    return {
        "role":                  env.get("_ROLE", ""),
        "discussion":            int(disc_raw) if disc_raw else None,
        "task_prompt":           env.get("_TASK", ""),
        "persona_voice":         psc.get("persona_voice", ""),
        "working_principles":    psc.get("working_principles", ""),
        "self_observe_gate":     psc.get("self_observe_gate", ""),
        "gate_line":             gate_line,
        "worktree_path":         json.loads(env.get("_WT_PATH") or "null"),
        # D#2014: set by spawn-agent.sh when isolation="worktree" was
        # requested but no concrete path could be resolved (e.g. a --pr
        # amend spawn where pr_tree_provision failed) — tells prompt_builder
        # to emit the honest "no tree" block instead of staying silent.
        "worktree_unprovisioned": bool(env.get("_WT_UNPROVISIONED", "")),
        # D#2222: WHY no path was resolved — "" (or "pr_tree_failed") means a
        # real provisioning attempt failed (hard stop is correct);
        # "agent_tool_provisions" means no attempt was made because none was
        # spawn-agent.sh's to make: --isolation worktree with no --pr/--worktree-path
        # is the canonical fresh-spawn shape, provisioned by the Agent tool's
        # own isolation param on the actual Agent() call.
        "worktree_unprovisioned_reason": env.get("_WT_UNPROVISIONED_REASON", ""),
        "security_block":        bool(env.get("_SEC", "")),
        "hook_event_id":         env.get("_EVENT_ID", ""),
        "env_scrub_snippet":     env.get("_ENV_SCRUB", ""),
        "prior_test_runs_block": env.get("_PRIOR_RUNS", ""),
        "dial_state_at_spawn":   env.get("_DIAL_STATE", ""),
        "pr":                    int(pr_raw) if pr_raw else None,
        "pr_branch":             env.get("_PR_BRANCH", ""),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: read the env vars build_payload() understands, print the JSON payload."""
    del argv  # no positional args — everything comes from the environment
    payload = build_payload(os.environ)
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
