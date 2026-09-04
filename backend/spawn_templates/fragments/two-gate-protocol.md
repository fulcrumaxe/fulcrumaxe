## TWO_GATE_PROTOCOL

Every implementation must pass BOTH gates before emitting `verdict: done`.

### Gate 1 — Spec-vs-Fixture (synthetic)
Run pytest / unit tests. These verify that your code matches the spec on
synthetic inputs. They do NOT prove the feature works in production.

```bash
pytest tests/ -x -q
```

### Gate 2 — Spec-vs-Reality (smoke)
Run the real binary, CLI, or UI against a live input. Capture the output.
This is the only way to confirm spec-vs-reality alignment.

**Required examples by surface:**
- **Backend endpoint**: `curl -s http://localhost:PORT/endpoint` → check response body
- **Dashboard**: start with `bash scripts/start-dashboard.sh`, open Chrome, navigate to the affected page, take screenshot or read console for errors
- **TUI**: `python3 -m dashboard_tui` with Pilot mode, or attach a screenshot
- **Scripts / shell tools**: execute the script directly, e.g. `bash scripts/my-script.sh`, capture stdout

**Gate 2 is N/A only with written justification**, e.g.:
> Gate 2 N/A: this change only modifies a template file injected at spawn time;
> no runnable binary or UI surface is affected.

**`verdict: done` is FORBIDDEN unless both gates pass** (or Gate 2 is N/A with justification).

### PR body requirement — EXACT canonical markers (HARD REQUIREMENT)

Every PR body MUST include a `## Verification` block. Each Gate line MUST contain
the literal token `PASS`, `PASSED`, `✓`, or `N/A` immediately after the colon.
The merge gate (`scripts/lib/two-gate-check.sh`) blocks the merge otherwise.

**Do NOT** write `Gate 2: 12 passed`, `Gate 2: 86%`, or `Gate 2: done` — a bare
count, percentage, or freeform word does NOT satisfy the gate and your PR will be
blocked at merge time, forcing manual body surgery.

Correct:
```
## Verification
Gate 1: PASS — N tests, 0 failures
Gate 2: PASS — `curl -s http://localhost:8000/health` returned {"status":"ok"}
```

or, for Gate 2 N/A:
```
## Verification
Gate 1: PASS — N tests, 0 failures
Gate 2: N/A — template-only change, no runnable surface affected
```
