---
name: update
description: Check whether this fulcrumaxe install is behind its upstream engine repo, and (once you know) name the exact command to bring it current.
---

`/update` answers one question honestly: is this install behind upstream, and if so, how do you fix it. This PR ships **detection only** — the check itself, and the exact manual command to apply an update today. An automated `/update --apply` (re-running bootstrap for you) is a separate, later PR; do not invent one here.

## Step 1 — run the check

```bash
bash scripts/update-check.sh
```

Read the exit code, not just the message — it's the actual verdict:

| Exit | Meaning |
|---|---|
| `0` | Up to date. Say so, plainly, and stop. |
| `10` | Update available. The message names how many commits behind. Continue to Step 2. |
| `20` | Cannot determine. The message carries a `reason=...` token — read it and relay the reason and the printed remedy to the user verbatim. Do **not** call this "up to date" and do **not** guess at a commit count. Stop here. |
| `2` | Usage error — this only happens if this command document itself is out of date relative to the script. Report the stderr message. |

Never paraphrase exit 20 as anything resembling "up to date." That distinction is the entire point of this command.

## Step 2 — if an update is available, name the command to apply it

This only runs when Step 1 exited `10`. You need `ENGINE_ROOT` to construct the apply command, and you must resolve it exactly the way `/coldstart` Step 0 does — copy that resolution logic, don't re-derive it ad hoc:

1. Look at the marker line: `${CLAUDE_PLUGIN_ROOT}`
   - If it resolved to a real absolute path (not the literal placeholder text), you're running as the installed plugin — that path is `ENGINE_ROOT`. Verify it the same way `/coldstart` does: `loop-bootstrap/bootstrap.sh`, `scripts/`, and `backend/` must all exist under it.
   - If it's still the literal unsubstituted placeholder, there's no plugin in play. Only trust this if you reached this text by *invoking* `/update` — reading this file directly off disk always shows the placeholder and tells you nothing (same caveat `/coldstart` documents, D#2214). In that case look for a fulcrumaxe engine clone on this machine (`git rev-parse --show-toplevel` from a known engine checkout, or ask the user).
2. If `ENGINE_ROOT` cannot be resolved with confidence by either path — **say so and stop**. Do not print an apply command you can't substantiate; a wrong `ENGINE_ROOT` in a printed command is worse than no command.
3. Once resolved and verified, print the literal command (substituting the real path, not a `$ENGINE_ROOT` variable reference — it won't survive to the user's own shell):

   ```bash
   bash "<ENGINE_ROOT>/loop-bootstrap/bootstrap.sh" --repo <owner/name> <this-repo's-local-path>
   ```

   Tell the user this re-runs the same idempotent population step `/coldstart` uses — it's safe to run again, and it will not touch `.autonomous-team/config.json`, `project.json`, `agent-profiles.json`, or anything under `$AUTONOMOUS_TEAM_STATE_DIR`.

4. After the user runs it, suggest re-running `bash scripts/update-check.sh` to confirm it now reports up to date.

## No baseline recorded?

If Step 1 reports `reason=no_baseline_recorded`, this install predates the baseline stamp (D#2335 PR 1) or was never bootstrapped through it. The remedy the script prints — `bash scripts/update-check.sh --record-baseline <sha>` — only works if you actually know a trustworthy baseline commit; when in doubt, re-running bootstrap (Step 2 above) writes a fresh, correct stamp as a side effect. Don't invent a SHA to make the message go away.
