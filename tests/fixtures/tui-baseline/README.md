# tui-baseline fixtures

Per-screen text captures from `bash scripts/tui-tester/tmux-sweep.sh`.

Bootstrap with:

```bash
bash scripts/tui-tester/tmux-sweep.sh --update-baselines
```

Files:
- `<screen>.txt` — raw tmux capture-pane output for each of the 11 screens
  (home, prs, discussions, loop, runs, agent_feed, stats, pr_detail,
   loop_controller, ideas, settings)

These are updated manually when the layout intentionally changes. On each
subsequent sweep run (without `--update-baselines`), the integration test
diffs the new capture against the stored baseline and fails if they differ.

Empty baselines (this initial state) mean no diff is performed — the test
only checks that captures are produced.
