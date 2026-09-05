---
name: update
description: Check whether this fulcrumaxe install is behind its upstream engine repo, and bring it current by re-running the engine's bootstrap over it.
---

`/update` answers one question honestly — is this install behind upstream — and then applies the update by re-running the engine's bootstrap over this tree. There is no second update mechanism: everything an apply writes is written by `loop-bootstrap/bootstrap.sh`.

## Step 1 — run the check

```bash
bash scripts/update-check.sh
```

Read the exit code, not just the message — it's the actual verdict:

| Exit | Meaning |
|---|---|
| `0` | Up to date. Say so, plainly, and stop. |
| `10` | Update available. The message names how many commits behind. Continue to Step 2. |
| `20` | Cannot determine. The message carries a `reason=...` token — read it and relay the reason and the printed remedy to the user verbatim. Do **not** call this "up to date" and do **not** guess at a commit count. You may still continue to Step 2 if the user wants to reinstall anyway (see "No baseline recorded?" below), but never describe that as applying a measured update. |
| `2` | Usage error — this only happens if this command document itself is out of date relative to the script. Report the stderr message. |

Never paraphrase exit 20 as anything resembling "up to date." That distinction is the entire point of this command.

## Step 2 — resolve `ENGINE_ROOT`

This runs when Step 1 exited `10` (or `20`, if the user wants to reinstall regardless). You need `ENGINE_ROOT` to apply anything, and you must resolve it exactly the way `/coldstart` Step 0 does — copy that resolution logic, don't re-derive it ad hoc. `scripts/update-apply.sh` deliberately cannot do this for you: `CLAUDE_PLUGIN_ROOT` is never exported into the shell a Bash tool call runs in, so only this document's own substituted marker line can tell you whether you are the installed plugin (D#2214).

1. Look at the marker line: `${CLAUDE_PLUGIN_ROOT}`
   - If it resolved to a real absolute path (not the literal placeholder text), you're running as the installed plugin — that path is `ENGINE_ROOT`. Verify it the same way `/coldstart` does: `loop-bootstrap/bootstrap.sh`, `scripts/`, and `backend/` must all exist under it.
   - If it's still the literal unsubstituted placeholder, there's no plugin in play. Only trust this if you reached this text by *invoking* `/update` — reading this file directly off disk always shows the placeholder and tells you nothing (same caveat `/coldstart` documents, D#2214). In that case look for a fulcrumaxe engine clone on this machine (`git rev-parse --show-toplevel` from a known engine checkout, or ask the user).
2. If `ENGINE_ROOT` cannot be resolved with confidence by either path — **say so and stop**. Do not run an apply you can't substantiate; a wrong `ENGINE_ROOT` is worse than none.

## Step 3 — preview the change set

```bash
bash scripts/update-apply.sh --engine-root "<the resolved absolute path>"
```

Substitute the literal path — a `$ENGINE_ROOT` variable reference won't survive between Bash tool calls.

The first run for a given engine commit **always** previews and writes nothing, whatever flags you pass. It prints every path that would be created or overwritten, exits `10`, and stops.

That preview is not a `--dry-run` arm of bootstrap. It's the real `bootstrap.sh`, no dry-run flag, run against a throwaway rsync mirror of this tree — so the paths it names come from the same code path the apply uses (D#2149: a preview computed by a different code path is not evidence about the apply). Two things it can't observe, and prints for itself: the mirror runs with `gh` de-authenticated so previewing can't create labels or open a team-log Issue, and the memory destination path is slug-normalized back to this tree.

Show the user the change set. If it looks wrong, stop — nothing has been written.

**Relay the "Upstream agent-definition updates this apply will NOT take:" section too, don't just relay the paths.** An apply does **not** take the upstream content of any `.claude/agents/*.md` file whose local copy differs from the engine's, and does **not** update `CLAUDE.md` at all after the first install — bootstrap preserves local overrides of both by design. The preview names each diverging agent file. A change set that looks complete but silently omits this reads as "your agent definitions were updated" when they weren't, so say plainly which files are being left alone. Taking those updates is a separate, explicit `--force` run of bootstrap, and it also discards any local edits to them — so it's the user's call, not yours.

## Step 4 — apply

Run the exact same command a second time. That invocation re-runs bootstrap against this repo for real, then re-measures with `update-check.sh` and reports what the install now is.

| Exit | Meaning |
|---|---|
| `0` | Applied, or there was nothing to apply. Relay the post-apply verdict it printed. |
| `10` | Still preview-only — a fresh preview was needed (e.g. the engine moved since the last one). Show it and ask again. |
| `20` | Could not proceed. Relay the `reason=...` token and the printed remedy verbatim. Nothing was written. |

An apply never touches `.autonomous-team/config.json`, `project.json`, `agent-profiles.json`, or anything under `$AUTONOMOUS_TEAM_STATE_DIR`, and never removes a file — so there's nothing for the Archive Protocol to catch.

If the post-apply check reports `update available` rather than `up to date`, that's not a failure: it means the engine tree at `ENGINE_ROOT` is itself behind upstream. Tell the user to bring the engine current and run `/update` again. `/update` deliberately does not fetch from the network on the user's behalf — that seam is what `scripts/engine-sync/` covers.

## No baseline recorded?

If Step 1 reports `reason=no_baseline_recorded`, this install predates the baseline stamp or was never bootstrapped through it. The remedy the script prints — `bash scripts/update-check.sh --record-baseline <sha>` — only works if you actually know a trustworthy baseline commit; when in doubt, applying (Steps 2–4) writes a fresh, correct stamp as a side effect. `update-apply.sh` will still preview and apply in this state, but it says out loud that it could not measure how far behind you were, and never calls that "up to date". Don't invent a SHA to make the message go away.
