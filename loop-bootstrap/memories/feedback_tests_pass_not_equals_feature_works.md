---
name: Tests-pass is not feature-works — separate verification axes
description: Synthetic fixture tests passing tells you the code matches the spec; running the actual binary/UI tells you the spec matches reality. Both gates required.
type: feedback
originSessionId: db7664de-2530-41cc-8214-e2c117f8188c
tier: transferable
---
A PR that ships green on `pytest` is NOT verified — the synthetic fixtures only cover shapes the author imagined. The actual feature can still be completely broken in production.

**Why:** observed at least 4 times in one session (2026-05-11):

1. **D#480 / PR #484** — loop-metrics writer shipped with `ts` field instead of `timestamp`. 13 tests passed. Real API call returned `{"timestamp": ""}` → chart can't render. (Caught by MCP browser tour.)
2. **D#486 / PR #489** — transcript classifiers shipped with 20 passing tests. Real run-analyst crashed with `AttributeError: 'str' object has no attribute 'get'` on first non-fixture transcript shape. (Caught by manually running `python3 backend/run_analyst.py --since=24h`.)
3. **D#493 / PR #499** — Loop Controller token form regression. Tests didn't cover the SSE auth path. Page still showed "Feed error: Unauthorized" in production. (Caught by MCP browser tour.)
4. **D#487 / PR #492** — `loop_metrics_counters.py` "fixed" `discussions_scanned` / `prs_scanned`. Tests passed against mocked snapshot. Real loop-metrics.jsonl rows still have 0 across all 100 entries. (Caught by API inspection.)

**How to apply:**

- **For UI/dashboard PRs**: D#497's visual-verification gate runs `browser-tester` agent against real pages, asserts no console errors / no "ApiError" strings / spec-defined elements rendered. Already enforced.

- **For CLI tool PRs**: before applying `code-review-passed`, the code-reviewer (or impl-coordinator after code-reviewer returns) must:
  1. Identify the CLI binary(ies) touched by the diff (search for `argparse`, `sys.argv`, `click`, `argv[1:]`).
  2. Run each binary against **representative real-world input** (not just fixtures), e.g. `python3 backend/run_analyst.py --since=24h` on actual `/tmp/claude-*/.../tasks/*.output` files.
  3. Capture the output (stdout, stderr, exit code) and include in the AGENT_OUTPUT `tests_run` array as `{command, exit_code, duration_seconds, real_world_input: true}`.
  4. If exit code != 0 or output shows wrong-shape error → verdict `needs-fix`, not `pass`.

- **For backend RPC handler PRs**: reviewer must call the new RPC method via `curl -X POST .../rpc` (with valid token) and check the response. Don't rely on a mock fixture for the upstream call.

- **For schema/config PRs**: reviewer must validate at least one real-world record against the new schema, not just hand-crafted positive/negative fixtures.

The unifying principle: **synthetic fixtures verify the code matches the spec; running the actual artifact verifies the spec matches reality.** Both gates required; one without the other ships broken features that pass CI.

## Also watch for: stale-base squash-merge regression

When a feature branch is built off a stale base and merged via squash, the squash can silently overwrite NEWER commits on main that touched the same function. PR #528 (Phase A.4 classifiers) was built before PR #527 (D#525 tool_output_ignored tightening) merged. The squash-merge of #528 brought its OLDER version of `classify_tool_output_ignored` along and stomped the tightening fix. Tests passed because PR #528's new tests didn't exercise the existing function.

**Reviewer rule:** after a PR adds/modifies a function that has been touched in main since the PR branch was created, verify the merged result contains all recent improvements. Concretely: for any PR touching a file changed in main within the last ~24h, the reviewer should run a behavior counter (line counts, classifier output counts, perf benchmarks) on the merged main and verify no regression vs. immediately-pre-merge main.

Tracked in Discussions: D#508 (CLI gate, 2026-05-11), D#530 (regression discovered, 2026-05-11).
