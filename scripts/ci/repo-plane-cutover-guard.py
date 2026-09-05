#!/usr/bin/env python3
"""CI guard: the repo-plane cutover cannot land while defects remain.

Setting ``code_repo`` in ``.autonomous-team/config.json`` or ``project.json``
is the repo-plane cutover — one config line that turns every remaining
mis-planed call site into a live misroute at the same instant: a security gate
that stops triggering, a CI kill switch that cannot be read, PR creation
against the wrong repo.

WHY THIS FILE EXISTS RATHER THAN JUST THE PYTEST
------------------------------------------------
The gate was written as ``tests/test_repo_plane_cutover_gate.py`` and, for one
review round, that was the whole of it. It was unreachable:

* ``ci.yml``'s backend job runs targeted ``scripts/ci/*`` guards, not pytest.
* ``preflight-fast.sh`` runs ``run_always_gates``, which does not run pytest
  either.
* ``run-pr-tests.sh`` routes a **config-only** PR to no suite at all — and a
  config-only PR is the exact shape of the change this guards against.

So the gate would have been green on the one PR it existed to stop. That is the
seventh instance in this series of a check that cannot fire, and it was in the
thing built to stop the other six. The guard belongs where the workflow already
looks: this directory, run unconditionally, with no path filter.

WHAT IT CHECKS
--------------
1. The detector's own self-test. A detector that has stopped detecting reports
   an empty defect list, which would clear the cutover — so its fixtures must
   still reproduce known real defects in all three languages before anything
   it says is believed.
2. The cutover gate: ``code_repo`` set while the ledger lists defects.
3. The ratchet: no new defect file, no grown count, no stale over-allowance.

Exit 0 = safe. Exit 1 = a real finding. Exit 2 = the guard could not tell,
which is a failure and never a pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DETECTOR = REPO_ROOT / "scripts" / "audit_repo_plane.py"


def _load_detector():
    if not DETECTOR.exists():
        print(f"FAIL: {DETECTOR} is missing — the repo-plane audit cannot run, "
              f"which is a failure and not an empty defect list.", file=sys.stderr)
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location("audit_repo_plane", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["audit_repo_plane"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    audit = _load_detector()

    # 1. Is the detector still a detector?
    if audit.run_self_test(verbose=False) != 0:
        print("FAIL: the repo-plane detector's self-test is red. Its fixtures "
              "no longer reproduce known real defects, so nothing it reports "
              "about this tree can be trusted — including a clean result.",
              file=sys.stderr)
        return 1
    print("ok: detector self-test passes "
          f"({len(audit.FIXTURES)} known positives, "
          f"{len(audit.NEGATIVE_FIXTURES)} negatives)")

    # 2/3. Cutover gate and ratchet.
    try:
        defects = [f for f in audit.scan_tree(REPO_ROOT) if f.is_defect]
        ledger = audit.load_baseline()
        violations = audit.cutover_violations(REPO_ROOT)
        regressions = audit.check_against_baseline(defects, ledger)
    except audit.LedgerError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    if violations:
        for v in violations:
            print(f"FAIL: {v}", file=sys.stderr)
        return 1

    if regressions:
        print("FAIL: the repo-plane defect ledger no longer matches the tree.",
              file=sys.stderr)
        for r in regressions:
            print(f"  - {r}", file=sys.stderr)
        print("\nFix the site, or — if it is genuinely new and accepted — "
              "update scripts/repo-plane-known-defects.txt in the same PR so "
              "the count stays honest.", file=sys.stderr)
        return 1

    remaining = sum(ledger.values())
    configured = audit.configured_code_repo(REPO_ROOT)
    print(f"ok: {remaining} known repo-plane defect(s) across {len(ledger)} "
          f"file(s), all recorded; code_repo "
          f"{'set: ' + configured[0][1] if configured else 'unset (pre-cutover)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
