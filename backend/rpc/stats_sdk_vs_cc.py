"""RPC handler: stats.sdk_vs_cc

Return per-role SDK vs CC comparison from agent_run data.

Response::

    {
      "rows": [
        {
          "role": "executor",
          "route": "sdk",
          "run_count": 42,
          "median_input_tok": 15000,
          "median_output_tok": 2000,
          "pass_rate": 0.857
        },
        ...
      ],
      "has_routed_via": true,
      "generated_at": "2026-05-20T14:33:00Z",
      "error": null
    }

When has_routed_via is false, rows is empty — the routed_via column has not been
added to agent_run yet (pre-D#1331 databases).
"""

from __future__ import annotations


def handle(params: dict) -> dict:
    """Return per-role SDK vs CC comparison.

    params: unused (no filter params for now — all roles, all time)
    """
    from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role  # noqa: PLC0415
    return sdk_vs_cc_by_role()
