---
name: coldstart
description: Onboard a target repo to the autonomous team from Muse — populate it, then provision it, in that order.
---

# Coldstart

Ported from `.claude/commands/coldstart.md` — that file remains canonical for Claude Code; this skill is the Muse adaptation. The Claude commands keep working exactly as now.

You are running the fulcrumaxe **engine** — a clone of the engine repo open in Muse — to set up **a different repo**, the adopter's own project, as an autonomous development team.

## Muse ground rules

- Work in the public repo checkout in front of you (the code plane). Never go looking for a private twin.
- There is no Claude CLI here and no spawn wrapper: implement every step directly yourself, inline, in this session.
- Bootstrap-first on fresh clones: a target with no state gets populated via `loop-bootstrap/bootstrap.sh --repo OWNER/NAME` before anything else touches it.
- For any Discussion or Spec content: restate-never-paste — summarize in your own words, never paste bodies verbatim.
- There is no plugin-marker substitution in Muse: no substituted text will appear in this document to tell you where the engine lives. Resolve the engine root explicitly per Step 0 and verify it — never fall back to searching the filesystem.

**Argument**: the target is either a GitHub repo slug the target has no local clone for yet (`OWNER/NAME` — clone it first with `git clone https://github.com/<slug> <local-dir>`, picking a sibling directory name from `<name>`), or a local path to an existing clone or an empty directory to scaffold fresh (`--mode new`).

If no target was given, ask the user for the target repo or path before doing anything else.

## Step 0 — find the engine root

Everything below runs from **the engine's own tree**, not the target repo. In Muse, `ENGINE_ROOT` is the engine checkout you have open (`git rev-parse --show-toplevel`, or the current working directory if invoked from the repo root). Verify it before proceeding — and verify it is not the target repo you are about to set up:

```bash
ENGINE_ROOT='<paste the resolved engine checkout path here>'
if [ ! -f "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" ] || [ ! -d "$ENGINE_ROOT/scripts" ] || [ ! -d "$ENGINE_ROOT/backend" ]; then
  echo "ERROR: '$ENGINE_ROOT' does not look like the fulcrumaxe engine tree — missing loop-bootstrap/bootstrap.sh, scripts/, or backend/. Stop here and report it — do not run coldstart from a tree that has not been verified to be the engine." >&2
  exit 1
fi
TARGET_PATH='<the local-target-path from the Argument section above>'
if [ "$(cd "$ENGINE_ROOT" && pwd)" = "$(cd "$TARGET_PATH" && pwd)" ]; then
  echo "ERROR: ENGINE_ROOT resolved to the same directory as the target repo ('$TARGET_PATH'). Everything in this document must run from the engine, not the target — re-resolve ENGINE_ROOT to the actual engine checkout." >&2
  exit 1
fi
```

Every path below is written relative to `ENGINE_ROOT`.

**`ENGINE_ROOT` does not survive between separate shell calls.** Each shell invocation starts fresh — a variable assigned in the guard block above is gone by the next call. Once you have resolved and verified `ENGINE_ROOT`, **write the literal absolute path itself into every subsequent command** in place of `$ENGINE_ROOT` — treat it below as a placeholder for you to substitute, not a reference to a live shell variable.

## What this does — and why order matters

`$ENGINE_ROOT/loop-bootstrap/bootstrap.sh` **populates** a repo (agent definitions, `CLAUDE.md`, `.claude/commands/`, `backend/`). `$ENGINE_ROOT/scripts/coldstart.sh` **provisions** the environment around it (state dir, dependencies, merge-gate labels, sandbox hook, HALT/interview, seed). Neither is sufficient alone — running provisioning first leaves a repo with a state dir and a `project.json` and nothing else: no agents, no `CLAUDE.md`, no `backend/`, even though the script itself exits 0. Population must run first.

## Step 1 — one command, both phases

Substitute the literal `ENGINE_ROOT` path you resolved and verified in Step 0 — `$ENGINE_ROOT` will not have a value in this shell call.

```bash
bash "$ENGINE_ROOT/scripts/coldstart-unified.sh" --repo <owner/name> \
    --path <local-target-path> --name <project-name> \
    --mode existing   # or: --mode new, for a brand-new empty project
```

This chains population and provisioning in the right order, with an ordering gate that fails loudly if population did not actually complete before provisioning starts. Add `--dry-run` first to see the full plan before it writes anything; add `--resume` later to skip straight to seeding once `epics/` is filled in (see Step 2).

If you need to run the two phases separately instead (e.g. to inspect state between them), the equivalent manual commands are:

```bash
bash "$ENGINE_ROOT/loop-bootstrap/bootstrap.sh" --repo <owner/name> <local-target-path>
bash "$ENGINE_ROOT/scripts/coldstart.sh" --path <local-target-path> --name <project-name> --mode existing
```

Read the output as you go — both phases are verbose on purpose. If either halts on a real error (dependency install failure, sandbox hook conflict, etc.), fix the named cause and re-run — all scripts involved are idempotent.

## Step 2 — the interview (HALT seam, you drive it)

Provisioning ends by handing off to you with an `orient` beat, then a `HANDOFF:` block naming the exact `scripts/coldstart-interview/harness.sh` subcommands to call as you drive the interview. **This is the existing, already-driven interview mechanism — do not rebuild it as a shell read loop.** Concretely:

1. Read the orient text the script printed — it is the mental model for the 9 interview topics (`mode identity stack deploy autonomy mission roster module_conventions backlog`).
2. For each remaining topic (`bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --coverage-check --session <id>` lists them), ask the user directly in chat, then persist the answer:
   ```bash
   bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --record-topic <topic> --answers '<json>' --session <id>
   ```
3. When coverage is complete, finish the session:
   ```bash
   bash "$ENGINE_ROOT/scripts/coldstart-interview/harness.sh" --finish-session --session <id>
   ```
4. The script then offers a tutorial and proceeds to seed the initial backlog from `epics/` if present.

**Never treat a `gh` 404 as "the repo doesn't exist".** `coldstart.sh` preflight already checked, before you ever reach this step, whether the active `gh` account can see the target repo if it has a github.com remote — a 404 there means the wrong account is active, not a missing repo, and preflight halts loudly over it. Never independently re-diagnose "missing repo" from a later 404 (e.g. failed label creation) and never offer to create the repo as a fix for it. And regardless of cause: never offer "create it as public" as an interview option, not even next to "create it as private" — publishing is irreversible, a wrong `gh` account is not. A genuinely-wanted public repo is a separate, deliberate action the operator takes themselves, outside this flow.

If the process halts anywhere else and prints a specific next command, run that command — a loud halt naming the exact next step is success, not failure. Never leave a silent partial state.

## Step 3 — GitHub label bootstrap (unattended)

`coldstart.sh` calls `scripts/bootstrap-github-labels.sh` automatically (unattended `gh label create --force` calls against whatever repo `git remote get-url origin` resolves to for the target). The only thing that stops it from running automatically is the target having no origin remote yet, in which case `coldstart.sh` prints a `WARN` naming the manual command. If you see that WARN, run it yourself once the target has a remote — again with the literal `ENGINE_ROOT` path substituted in, not the variable:

```
bash "$ENGINE_ROOT/scripts/bootstrap-github-labels.sh" --repo <owner/name>
```

## Step 4 — report what's left

After the run, enumerate any step that is neither automated nor already printed as an explicit next action. At minimum check:

- Did `gates.allow_claude_spawn` end up in `.autonomous-team/config.json`? (The population step installs a conservative default of `false`; the dashboard backend will boot, but spawning stays gated off until the user flips it.)
- Were labels created, or does the user still need to run the manual command above?
- Does `epics/` have real content yet, or is the backlog seed step still pending a `--resume` run?

Tell the user plainly what is done and what (if anything) they still need to do — no silent partial state.
