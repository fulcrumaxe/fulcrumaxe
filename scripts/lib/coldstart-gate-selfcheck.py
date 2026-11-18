#!/usr/bin/env python3
"""Coldstart's public-repo check that the external-intake gate is live.

The gate (external_intake_gate.py) has no on/off switch. For a public repo
coldstart does not trust that: it asks the gate about a synthetic author who
is not on the collaborator allowlist and requires a refusal. Exit 0 only when
the gate blocks that author; anything else, including an import failure,
exits 1.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The gate imports `backend._repo` from the engine root.
sys.path.insert(1, os.path.dirname(os.path.dirname(_HERE)))

STRANGER = "coldstart-selfcheck-non-collaborator"
TRUSTED = {"coldstart-selfcheck-collaborator"}


def main() -> int:
    # The gate resolves a repo slug when imported. The check below never uses
    # it (it is a pure function of author, labels and allowlist), and no
    # project.json exists yet at preflight, so give it a placeholder.
    os.environ.setdefault("AUTONOMOUS_TEAM_REPO", "selfcheck/selfcheck")
    try:
        import external_intake_gate as gate

        provenance = gate.classify_provenance(STRANGER, TRUSTED)
        blocked, reason = gate.should_block_spawn(STRANGER, [], TRUSTED)
    except Exception as exc:  # noqa: BLE001 - any failure means "not proven"
        print(f"gate self-check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if provenance != gate.PROVENANCE_EXTERNAL or blocked is not True:
        print(f"gate self-check failed: stranger not refused ({reason})", file=sys.stderr)
        return 1
    print(f"gate self-check ok: stranger refused ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
