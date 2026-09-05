#!/usr/bin/env python3
"""pilot-sweep.py — Layer A: Textual Pilot screenshot sweep (in-process).

Runs backend.tui_tester_helpers.run_verification() against the real STATE_DIR
(mounted read-only via AUTONOMOUS_TEAM_STATE_DIR env override).

Writes per-screen widget-tree JSON under:
  ~/.autonomous-forever-state/tui-tester/<run-id>/tab-<key>.tree.json

Writes summary:
  ~/.autonomous-forever-state/tui-tester/<run-id>/findings.json

Shape of findings.json:
  { "verdict": "pass" | "needs-fix" | "fail",
    "findings": [...],        # list of finding dicts from run_verification()
    "artifact_dir": "...",    # absolute path to per-run artifact dir
    "elapsed_s": 12.3 }

Usage:
  python3 scripts/tui-tester/pilot-sweep.py [--state-dir /path] [--timeout 15]

Exits 0 if verdict==pass, 1 if needs-fix, 2 if fail.

The STATE_DIR is treated as read-only: this script never writes to it.
Artifacts are stored in a NEW subdirectory STATE_DIR/tui-tester/<run-id>/
which is created at sweep time (mode 0700) and is NOT pre-existing state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so `backend` package is importable when the
# script is invoked directly (not via python3 -m).
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/tui-tester/
_REPO_ROOT = _SCRIPT_DIR.parent.parent                  # repo root


def _add_repo_to_path() -> None:
    root_str = str(_REPO_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_add_repo_to_path()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Layer A: Pilot-driven TUI screenshot sweep.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-dir",
        help="Override AUTONOMOUS_TEAM_STATE_DIR (default: ~/.autonomous-forever-state)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Wall-time timeout in seconds (default: 15). Sweep is aborted if exceeded.",
    )
    args = parser.parse_args(argv)

    # Apply state-dir override before importing backend.state_paths
    if args.state_dir:
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = args.state_dir
        print(f"[pilot-sweep] STATE_DIR overridden to: {args.state_dir}", flush=True)

    # Validate STATE_DIR is readable
    from backend.state_paths import STATE_DIR  # imported after env override

    if not STATE_DIR.exists():
        print(
            f"[pilot-sweep] WARNING: STATE_DIR does not exist: {STATE_DIR} — sweep will run against empty state",
            flush=True,
        )

    print(f"[pilot-sweep] STATE_DIR={STATE_DIR}", flush=True)
    print(f"[pilot-sweep] starting sweep (timeout={args.timeout}s) …", flush=True)

    t0 = time.monotonic()

    # Run the sweep from run_verification (Pilot mode — full app render + checks)
    try:
        from backend.tui_tester_helpers import run_verification
        from backend.tui_tester_consistency import check_all as consistency_check_all

        result = run_verification(repo_root=_REPO_ROOT)

        # Cross-screen consistency pass: build a pilot_screens dict from findings
        # artifact_dir contains per-tab widget-tree JSON files; we reconstruct
        # text from the visible_text fields as a best-effort screen snapshot.
        artifact_dir_path = result.get("artifact_dir", "")
        if artifact_dir_path:
            import json as _json
            from pathlib import Path as _Path
            pilot_screens: dict = {}
            _adir = _Path(artifact_dir_path)
            for tree_file in _adir.glob("tab-*.tree.json"):
                # Derive screen key from filename: "tab-1.tree.json" → key from findings
                # We can also use the tab name embedded in the findings.
                screen_key = tree_file.stem.replace("tab-", "").replace(".tree", "")
                # Map single-char keys to tab ids via findings
                for finding in result.get("findings", []):
                    ev = finding.get("evidence_path", "") or ""
                    if str(tree_file) in ev or tree_file.name in ev:
                        screen_key = finding.get("tab", screen_key)
                        break
                try:
                    widgets = _json.loads(tree_file.read_text(encoding="utf-8"))
                    texts = " | ".join(
                        w.get("visible_text", "") for w in widgets
                        if w.get("visible_text")
                    )
                    pilot_screens[screen_key] = texts
                except Exception:
                    pass
            consistency_violations = consistency_check_all(pilot_screens)
            if consistency_violations:
                result["findings"].extend(consistency_violations)
                # Downgrade verdict if currently passing
                if result.get("verdict") == "pass":
                    result["verdict"] = "needs-fix"
    except Exception as exc:
        elapsed = time.monotonic() - t0
        result = {
            "verdict": "fail",
            "findings": [
                {
                    "tab": "_sweep",
                    "widget_id": "_sweep",
                    "check_name": "pilot_sweep_error",
                    "status": "fail",
                    "evidence_path": None,
                    "detail": str(exc),
                }
            ],
            "artifact_dir": "",
            "elapsed_s": elapsed,
        }

    elapsed = time.monotonic() - t0
    result["elapsed_s"] = elapsed

    # Enforce timeout — if we got here but exceeded wall time, downgrade verdict
    if elapsed > args.timeout and result.get("verdict") == "pass":
        result["verdict"] = "needs-fix"
        result["findings"].append(
            {
                "tab": "_sweep",
                "widget_id": "_sweep",
                "check_name": "sweep_timeout",
                "status": "fail",
                "evidence_path": None,
                "detail": f"Sweep took {elapsed:.1f}s, exceeded timeout={args.timeout}s",
            }
        )

    # Write findings.json into the artifact dir (already created by run_verification)
    artifact_dir_str = result.get("artifact_dir", "")
    if artifact_dir_str:
        artifact_dir = Path(artifact_dir_str)
        findings_path = artifact_dir / "findings.json"
        try:
            findings_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[pilot-sweep] findings written to: {findings_path}", flush=True)
        except OSError as exc:
            print(f"[pilot-sweep] WARNING: could not write findings.json: {exc}", flush=True)

    # Summary to stdout
    verdict = result.get("verdict", "fail")
    finding_count = len(result.get("findings", []))
    fail_count = sum(
        1 for f in result.get("findings", []) if f.get("status") == "fail"
    )
    print(
        f"[pilot-sweep] done: verdict={verdict} findings={finding_count} "
        f"failures={fail_count} elapsed={elapsed:.1f}s",
        flush=True,
    )

    # Emit compact JSON to stdout for machine consumption
    print(json.dumps(result, ensure_ascii=False))

    if verdict == "pass":
        return 0
    elif verdict == "needs-fix":
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())
