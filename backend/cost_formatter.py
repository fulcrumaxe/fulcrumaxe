"""backend/cost_formatter.py

Formats cost_tracker by-discussion JSON output into a markdown table
suitable for posting as a Discussion comment.

Usage (library):
    from backend.cost_formatter import format_cost_table
    md = format_cost_table(data)   # data = dict from cost_tracker by-discussion

Usage (CLI — pipe from cost_tracker):
    python3 backend/cost_tracker.py by-discussion --discussion 42 | python3 backend/cost_formatter.py
"""

from __future__ import annotations

import json
import sys
from typing import Union


def format_cost_table(data: dict) -> str:
    """Format a cost_tracker by-discussion entry as a GitHub-flavoured markdown table.

    Args:
        data: Dict returned by cost_tracker ``by-discussion --discussion N``.
              Expected keys: ``discussion``, ``total_cost_usd``,
              ``total_input_tokens``, ``total_output_tokens``,
              ``agent_count``, ``agent_breakdown`` (role -> cost_usd).

    Returns:
        Markdown string, or empty string when total_cost_usd is 0 or data is empty.

    The function is intentionally defensive — missing or malformed fields are
    treated as zero rather than raising an exception.
    """
    if not data or not isinstance(data, dict):
        return ""

    total_cost = float(data.get("total_cost_usd", 0.0) or 0.0)
    if total_cost <= 0.0:
        return ""

    discussion = data.get("discussion", "?")
    total_input = int(data.get("total_input_tokens", 0) or 0)
    total_output = int(data.get("total_output_tokens", 0) or 0)
    agent_count = int(data.get("agent_count", 0) or 0)
    agent_breakdown: dict = data.get("agent_breakdown", {}) or {}

    lines: list[str] = []
    lines.append(
        f"**Cost summary for Discussion #{discussion}** "
        f"— {agent_count} agent run{'s' if agent_count != 1 else ''}"
    )
    lines.append("")
    lines.append("| Role | Input tokens | Output tokens | Cost (USD) |")
    lines.append("|------|-------------:|--------------:|-----------:|")

    # Sort roles by cost descending so the biggest spender is first
    sorted_roles = sorted(
        agent_breakdown.items(),
        key=lambda kv: float(kv[1] or 0),
        reverse=True,
    )

    for role, role_cost in sorted_roles:
        role_cost_f = float(role_cost or 0)
        if role_cost_f <= 0.0:
            continue
        # We don't have per-role token counts in agent_breakdown — only the total.
        # Show "—" for per-role token columns; only the totals row has real numbers.
        lines.append(
            f"| {role} | — | — | ${role_cost_f:.4f} |"
        )

    # Total row
    lines.append(
        f"| **Total** | {total_input:,} | {total_output:,} | **${total_cost:.4f}** |"
    )

    return "\n".join(lines)


def _main() -> int:
    """CLI entry point: read JSON from stdin, print formatted markdown."""
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"cost_formatter: JSON parse error — {exc}", file=sys.stderr)
        return 1

    md = format_cost_table(data)
    if md:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
