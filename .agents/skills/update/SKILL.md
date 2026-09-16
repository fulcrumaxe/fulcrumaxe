---
name: update
description: Check whether this install is behind its upstream engine repo, and bring it current from Muse.
---

# Update

Ported from `.claude/commands/update.md` — that file remains canonical for Claude Code; this skill is the Muse adaptation. The Claude commands keep working exactly as now.

`/update` answers one question honestly — is this install behind upstream — and then applies the update by re-running the engine's bootstrap over this tree. There is no second update mechanism: everything an apply writes is written by `loop-bootstrap/bootstrap.sh`.

## Muse ground rules

- Work in the public repo checkout in front of you (the code plane). Never go looking for a private twin.
- There is no Claude CLI here and no spawn wrapper: implement every step directly yourself, inline, in this session.
- On a fresh clone with no state yet, populate first: `loop-bootstrap/bootstrap.sh --repo OWNER/NAME <local-path>` (bootstrap-first), then return here.
- For any Discussion or Spec content: restate-never-paste — summarize in your own words, never paste bodies verbatim.
- There is no plugin-marker substitution in Muse: no substituted text will appear in this document to tell you where the engine lives. Resolve the engine root explicitly per Step 2 and verify it — never fall back to searching the filesystem.

## Step 1 — run the check

```bash
bash scripts/update-check.sh
```

Read the exit code, not just the message — it is the actual verdict:

| Exit | Meaning |
|---|---|
| `0` | Up to date. Say so, plainly, and stop. |
| `10` | Update available. The message names how many commits behind. Continue to Step 2. |
| `20` | Cannot determine. The message carries a `reason=...` token — read it and relay the reason and the printed remedy to the user verbatim. Do **not** call this "up to date" and do **not** guess at a commit count. You may still continue to Step 2 if the user wants to reinstall anyway (see "No baseline recorded?" below), but never describe that as applying a measured update. |
| `2` | Usage error — this only happens if this skill document itself is out of date relative to the script. Report the stderr message. |

Never paraphrase exit 20 as anything resembling "up to date." That distinction is the entire point of this skill.

## Step 2 — resolve `ENGINE_ROOT`

This runs when Step 1 exited `10` (or `20`, if the user wants to reinstall regardless). In Muse, `ENGINE_ROOT` is the engine checkout you have open (`git rev-parse --show-toplevel` from a known engine checkout, or ask the user). Only trust a path you resolved this way — reading this file off disk tells you nothing about where the engine lives. Verify the tree IS the engine before proceeding:

```bash
ENGINE_ROOT='<paste the resolved engine checkout path here>'
if [ ! -f "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" ] || [ ! -d "$ENGINE_ROOT/scripts" ] || [ ! -d "$ENGINE_ROOT/backend" ]; then
  echo "ERROR: '$ENGINE_ROOT' does not look like the fulcrumaxe engine tree — missing loop-bootstrap/bootstrap.sh, scripts/, or backend/. Stop here and report it — do not run an apply from a tree that has not been verified to be the engine." >&2
  exit 1
fi
```

If `ENGINE_ROOT` cannot be resolved with confidence — **say so and stop**. Do not run an apply you can't substantiate; a wrong `ENGINE_ROOT` is worse than none.

## Step 3 — preview the change set

```bash
bash scripts/update-apply.sh --engine-root "<the resolved absolute path>"
```

Substitute the literal path — a `$ENGINE_ROOT` variable reference won't survive between shell calls.

The first run for a given engine commit **always** previews and writes nothing, whatever flags you pass. It prints every path that would be created or overwritten, exits `10`, and stops.

That preview is not a dry-run arm of bootstrap. It is the real `bootstrap.sh`, no dry-run flag, run against a throwaway rsync mirror of this tree — so the paths it names come from the same code path the apply uses. Two things it can't observe, and prints for itself: the mirror runs with `gh` de-authenticated so previewing can't create labels or open a team-log Issue, and the memory destination path is slug-normalized back to this tree.

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

If the post-apply check reports `update available` rather than `up to date`, that's not a failure: it means the engine tree at `ENGINE_ROOT` is itself behind upstream. Tell the user to bring the engine current and run the update again. The update deliberately does not fetch from the network on the user's behalf — that seam is what `scripts/engine-sync/` covers.

## No baseline recorded?

If Step 1 reports `reason=no_baseline_recorded`, this install predates the baseline stamp or was never bootstrapped through it. The remedy the script prints — `bash scripts/update-check.sh --record-baseline <sha>` — only works if you actually know a trustworthy baseline commit; when in doubt, applying (Steps 2–4) writes a fresh, correct stamp as a side effect. `update-apply.sh` will still preview and apply in this state, but it says out loud that it could not measure how far behind you were, and never calls that "up to date". Don't invent a SHA to make the message go away.
