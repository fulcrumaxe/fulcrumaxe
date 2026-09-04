---
name: coldstart
description: One-command onboarding — populate a target repo with the autonomous team, then provision it (state, deps, labels, sandbox hook, interview), in that order.
---

You are running the fulcrumaxe **engine** — either an installed plugin or a clone of the engine repo — to set up **a different repo**, the adopter's own project, as an autonomous development team.

**Argument**: `$ARGUMENTS` is either

- a GitHub repo slug the target doesn't have a local clone for yet (`owner/name`) — clone it first (`git clone https://github.com/<slug> <local-dir>`, picking a sibling directory name from `<name>`), or
- a local path to an existing clone or an empty directory to scaffold fresh (`--mode new`).

If `$ARGUMENTS` is empty, ask the user for the target repo or path before doing anything else.

## Step 0 — find the engine root

Everything below runs from **the engine's own tree**, not the target repo. Resolve `ENGINE_ROOT` from how this document reached you — not by reading an environment variable at runtime. The `CLAUDE_PLUGIN_ROOT` variable Claude Code exports to hook, MCP, and LSP subprocesses is **never exported into the shell your Bash tool runs in** — `echo $CLAUDE_PLUGIN_ROOT` from Bash comes back empty even when you *are* running as the installed plugin. Reading it that way is exactly the bug D#2214 filed: on a clean machine the agent finds nothing and burns a minute searching the filesystem; on a machine that also happens to have an engine clone, the search silently finds *that* instead and the plugin install gets bypassed with no error at all.

The reliable signal doesn't touch the shell. Claude Code substitutes the `CLAUDE_PLUGIN_ROOT` placeholder **inline, in this document's own text, at the moment the command loads** — before you ever see it — and that substitution only fires for a plugin's own command/agent/skill content. So the line right below tells you which case you're in, just by what it says:

**Engine root marker:** `${CLAUDE_PLUGIN_ROOT}`

**This only works if you got here by invoking the command — not by reading the file.** Substitution happens when Claude Code *loads* `/coldstart` into context, not when something merely `cat`s or greps this file off disk. If you (or an agent orienting itself) read `coldstart.md` directly from the filesystem instead of running the command, the marker line above will always show the literal, unsubstituted text — indistinguishable from the real "no plugin in play" case below — even on a machine where the plugin *is* installed. Seeing the literal placeholder is evidence of "not a plugin" **only** when you reached this text by invocation; if you reached it by reading the file, it tells you nothing, and falling back to a clone search from that reading would reproduce the exact D#2214 bug this fix exists to close. (Confirmed live during D#2214 verification: reading the file off disk first showed the literal placeholder and would have sent the agent down the wrong branch had it trusted that reading.)

- If the marker line above now shows an absolute filesystem path — not the literal text `CLAUDE_PLUGIN_ROOT` — you're running as the installed plugin (`/fulcrumaxe:coldstart`). That path *is* `ENGINE_ROOT`; copy it verbatim, don't re-derive it. `loop-bootstrap/`, `scripts/`, and `backend/` all ship inside the plugin, at that path. Before proceeding, sanity-check it — this is the hard-error check D#2214 asked for, and it must refuse rather than fall back to a search:

  ```bash
  # ENGINE_ROOT is the literal path the marker line resolved to above — paste it in, don't re-read an env var:
  ENGINE_ROOT='<paste the resolved marker path here>'
  # Content check, not a path-shape check: a layout test (e.g. matching */plugins/*)
  # false-errors on a legitimately loaded plugin scaffolded by the plugin-authoring
  # init workflow, which lands under ~/.claude/skills/<name>/ with no /plugins/
  # segment at all and still resolves and loads correctly. Assert the tree IS the
  # engine instead, regardless of where it happens to sit on disk:
  if [ ! -f "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" ] || [ ! -d "$ENGINE_ROOT/scripts" ] || [ ! -d "$ENGINE_ROOT/backend" ]; then
    echo "ERROR (D#2214 guard): running as the installed plugin (/fulcrumaxe:coldstart) but '$ENGINE_ROOT' is missing loop-bootstrap/bootstrap.sh, scripts/, or backend/ — it does not look like the fulcrumaxe engine tree. This is the exact failure mode D#2214 filed: a wrong-but-plausible ENGINE_ROOT silently bypassing the plugin in favor of something else on disk. Stop here and report it to the user — do not fall back to searching the filesystem for an engine clone." >&2
    exit 1
  fi
  ```

- If the marker line still reads literally as the placeholder text (unsubstituted — no real path appeared), there's no plugin in play: you're running from a clone of the `fulcrumaxe` engine repo opened directly in Claude Code. `ENGINE_ROOT` is that clone's root (`git rev-parse --show-toplevel`, or the current working directory if this command was invoked from the repo root).

  This branch is reached by inference rather than by a substituted marker, and `git rev-parse --show-toplevel` run inside an adopter's own project resolves to the TARGET repo, not the engine — directly contradicting the opening sentence of this section. Run the same content check as the plugin branch above before proceeding, plus one more: that the resolved tree isn't the target repo you're about to set up.

  ```bash
  # ENGINE_ROOT is the literal path `git rev-parse --show-toplevel` (or your cwd) resolved to:
  ENGINE_ROOT='<paste the resolved clone root path here>'
  # Same content check as the plugin branch — assert the tree IS the engine:
  if [ ! -f "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" ] || [ ! -d "$ENGINE_ROOT/scripts" ] || [ ! -d "$ENGINE_ROOT/backend" ]; then
    echo "ERROR: '$ENGINE_ROOT' does not look like the fulcrumaxe engine tree — missing loop-bootstrap/bootstrap.sh, scripts/, or backend/. Stop here and report it to the user — do not run coldstart-unified.sh from a tree that hasn't been verified to be the engine." >&2
    exit 1
  fi
  # And that it isn't the target repo you're about to set up — a bare
  # `git rev-parse --show-toplevel` run from inside the target defaults there:
  TARGET_PATH='<the local-target-path from the Argument section above>'
  if [ "$(cd "$ENGINE_ROOT" && pwd)" = "$(cd "$TARGET_PATH" && pwd)" ]; then
    echo "ERROR: ENGINE_ROOT resolved to the same directory as the target repo ('$TARGET_PATH'). Everything in this document must run from the engine, not the target — re-resolve ENGINE_ROOT to the actual engine clone." >&2
    exit 1
  fi
  ```

Every path below is written relative to `ENGINE_ROOT`.

**`ENGINE_ROOT` does not survive between separate Bash tool calls.** Each Bash tool invocation starts a fresh, non-persistent shell — a variable assigned in the guard block above is gone by the time the next Bash call runs, including Step 1 and Step 3 below. Reading `$ENGINE_ROOT` there comes back empty. Do not treat that as a signal to re-derive the value by searching the filesystem — an agent that does so reintroduces the exact D#2214 bug this document exists to close. Instead, once you've resolved and verified `ENGINE_ROOT` above, **write the literal absolute path itself into every subsequent command** in place of `$ENGINE_ROOT` — treat it below as a placeholder for you to substitute, not a reference to a live shell variable.

## What this does — and why order matters

`$ENGINE_ROOT/loop-bootstrap/bootstrap.sh` **populates** a repo (23 agent definitions, `CLAUDE.md`, `.claude/commands/`, `backend/`). `$ENGINE_ROOT/scripts/coldstart.sh` **provisions** the environment around it (state dir, dependencies, merge-gate labels, sandbox hook, HALT/interview, seed). Neither is sufficient alone — running provisioning first leaves a repo with a state dir and a `project.json` and nothing else: no agents, no `CLAUDE.md`, no `backend/`, even though the script itself exits 0. Population must run first. This is measured, not assumed (D#1872).

`loop-bootstrap/` ships in the engine clone, the open-source export, and the installed plugin alike — all three are `ENGINE_ROOT` candidates above, so there's no case where this command finds itself without it.

## Step 1 — one command, both phases

Substitute the literal `ENGINE_ROOT` path you resolved and verified in Step 0 — `$ENGINE_ROOT` will not have a value in this Bash call.

```bash
bash "$ENGINE_ROOT/scripts/coldstart-unified.sh" --repo <owner/name> \
    --path <local-target-path> --name <project-name> \
    --mode existing   # or: --mode new, for a brand-new empty project
```

This chains population and provisioning in the right order for you, with an ordering gate that fails loudly if population didn't actually complete before provisioning starts (D#1872). Add `--dry-run` first to see the full plan before it writes anything; add `--resume` later to skip straight to seeding once `epics/` is filled in (see Step 2).

If you need to run the two phases separately instead (e.g. to inspect state between them), the equivalent manual commands are:

```bash
bash "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" --repo <owner/name> <local-target-path>
bash "$ENGINE_ROOT/scripts/coldstart.sh" --path <local-target-path> --name <project-name> --mode existing
```

Read the output as you go — both phases are verbose on purpose. If either halts on a real error (dependency install failure, sandbox hook conflict, etc.), fix the named cause and re-run — all scripts involved are idempotent.

## Step 2 — the interview (HALT seam, agent-driven)

Provisioning ends by handing off to you with an `orient` beat, then a `HANDOFF:` block naming the exact `scripts/coldstart-interview/harness.sh` subcommands to call as you drive the interview. **This is the existing, already-agent-driven interview mechanism — do not rebuild it as a shell read loop.** Concretely:

1. Read the orient text the script printed — it's the mental model for the 9 interview topics (`mode identity stack deploy autonomy mission roster module_conventions backlog`).
2. For each remaining topic (`bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --coverage-check --session <id>` lists them), ask the user with `AskUserQuestion`, then persist the answer:
   ```bash
   bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --record-topic <topic> --answers '<json>' --session <id>
   ```
3. When coverage is complete, finish the session:
   ```bash
   bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --finish-session --session <id>
   ```
4. The script then offers a tutorial and proceeds to seed the initial backlog from `epics/` if present.

**Never treat a `gh` 404 as "the repo doesn't exist" (D#2227).** `coldstart.sh` preflight already checked, before you ever reach this step, whether the active `gh` account can see the target repo if it has a github.com remote — a 404 there means the wrong account is active, not a missing repo, and preflight halts loudly over it rather than letting you get this far. If you're at Step 2 at all, that's already settled. Never independently re-diagnose "missing repo" from a later 404 (e.g. failed label creation) and never offer to create the repo as a fix for it. And regardless of cause: never offer "create it as public" as an interview option, not even next to "create it as private" — publishing is irreversible, a wrong `gh` account is not. A genuinely-wanted public repo is a separate, deliberate action the operator takes themselves, outside this flow.

If the process halts anywhere else and prints a specific next command, run that command — a loud halt naming the exact next step is success, not failure. Never leave a silent partial state.

## Step 3 — GitHub label bootstrap (unattended, not dial-gated)

`coldstart.sh` calls `scripts/bootstrap-github-labels.sh` automatically, which runs 8 `gh label create --force` calls unattended against whatever repo `git remote get-url origin` resolves to for the target — there is no `external.system` / control-plane dial check on this call, at any dial level (pre-existing, not specific to this command). The only thing that stops it from running automatically is the target having no origin remote yet, in which case `coldstart.sh` prints a `WARN` naming the manual command. If you see that WARN, run it yourself once the target has a remote — again with the literal `ENGINE_ROOT` path substituted in, not the variable:

```
bash "$ENGINE_ROOT/scripts/bootstrap-github-labels.sh" --repo <owner/name>
```

## Step 4 — report what's left

After the run, enumerate any step that is neither automated nor already printed as an explicit next action (see D#1872 item 19 — this list is the actual deliverable, not an afterthought). At minimum check:

- Did `gates.allow_claude_spawn` end up in `.autonomous-team/config.json`? (The population step — `loop-bootstrap/bootstrap.sh` step 20 — installs a conservative default of `false`, whether you ran it through `coldstart-unified.sh` or by hand; the dashboard backend will boot, but spawning stays gated off until the user flips it.)
- Were labels created, or does the user still need to run the manual command above?
- Does `epics/` have real content yet, or is the backlog seed step still pending a `--resume` run?

Tell the user plainly what's done and what (if anything) they still need to do — no silent partial state.
