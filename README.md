# fulcrumaxe

![CI](https://github.com/fulcrumaxe/fulcrumaxe/actions/workflows/ci.yml/badge.svg)

fulcrumaxe is an autonomous software team that runs in your GitHub repo, turns Discussions into merged PRs, and spends its own idle cycles improving itself. This README is the operating manual — it covers everything from installing prerequisites through what to do when something breaks.

## Status

This is an experimental, work-in-progress project, not a finished product. It runs daily on its own repo and ships real PRs, but the codebase has rough edges: overlapping scripts, docs that lag behind code, and conventions that haven't been consolidated yet. Expect it to keep changing shape as it improves itself — there's no fixed end state.

What "self-improving" looks like in practice: when the Discussion queue runs dry, the loop spawns a quality-sweep or mission-analyst agent to scan the codebase and file its own Discussions for things it noticed — a stale check, a missing dependency, an inconsistency between two scripts. Those go through the same Discussion → Spec → PR → review → merge pipeline as anything a human requests, self-identified and self-fixed, no human writing the code. If you want to help organize, consolidate, or polish any of the rough edges — agent role definitions, the dashboard, the scripts directory — open a Discussion (see [Contributing](#contributing)) and dig in.

## Why this exists

fulcrumaxe is a self-hosted AI development environment built for one developer running their own infrastructure, not a multi-tenant SaaS. You get an operations console (the dashboard), a team of specialized agent roles that coordinate through GitHub Discussions and PRs, and a loop that keeps proposing and shipping improvements without you having to babysit every step. If you want to hand off routine implementation work — bug fixes, small features, docs — while staying the one who decides direction, this is built for you.

This repo (`fulcrumaxe`) is the real thing — full internal history, not a mirror or export target. It also holds internal-only material (training tooling, experiments) that doesn't ship publicly. A separate curated export (`open-source/export.sh` + `open-source/MANIFEST.md`) copies only the paths marked ready in the manifest out to the public release.

## Prerequisites

- **[Claude Code](https://claude.com/product/claude-code)** — the agent runtime this whole project is built on. Every role (Team Lead, executor, code-reviewer, and the rest) is a Claude Code session or sub-agent working from this repo's `CLAUDE.md`. Install it and make sure `claude` is on your `PATH` before doing anything else here.
- **A Claude credential** — one of `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or a `claude` CLI login already stored (run `claude login` once, interactively, and it's picked up automatically). You bring your own subscription or API key; nothing here provisions one for you.
- **`gh` CLI, authenticated** — `gh auth login`. Everything routes through GitHub Discussions, Issues, and PRs.
- **Node.js and Python 3** — versions aren't pinned for local use, but CI runs Node.js 20 and Python 3.12 (see `.github/workflows/ci.yml`); anything reasonably close should work. `scripts/lib/coldstart-preflight.sh` checks that `gh`, `node`, and `python3` are *present* before doing anything else — it does not check versions, so a mismatch won't be caught for you.

## Provisioning: coldstart

### Marketplace install (one command, no clone)

```
/plugin marketplace add fulcrumaxe/fulcrumaxe
/plugin install fulcrumaxe@fulcrumaxe
/coldstart --path /path/to/your/repo --name your-project
```

This installs fulcrumaxe as a Claude Code plugin — the 26 agent role definitions, the `/coldstart` and `/start-the-day` commands, and everything they call (`backend/`, `scripts/`, `hooks/`, `loop-bootstrap/`) all ship inside the plugin, so there's nothing to `git clone` first. `/coldstart` runs the exact same two-phase population-then-provisioning sequence described below against whatever `--path` you give it.

What this route does **not** change: you still need the [prerequisites](#prerequisites) (Claude Code, a Claude credential, authenticated `gh`, Node/Python), your target still needs a real GitHub `origin` remote before a real (non-`--dry-run`) run, and the interview/label/report steps are identical either way. It also doesn't remove the need for a persistent engine checkout for day-to-day operation — running the dashboard (`AF_ROOT`, see [Updating](#updating)) and keeping `/loop` running long-term is still most convenient from a real clone of this repo, not the plugin's own install directory. This route replaces the clone-and-run-a-script onboarding step below; it isn't a different, lighter-weight product.

### From a clone

```bash
git clone https://github.com/fulcrumaxe/fulcrumaxe.git
cd fulcrumaxe
bash scripts/coldstart.sh --path /path/to/your/repo --name your-project --dry-run
```

`--dry-run` prints the ordered plan and touches nothing — no files, no state dir, no GitHub API calls. Read it before running for real.

Provisioning is two phases, and the first run only gets you through the first one:

```bash
bash scripts/coldstart.sh --path /path/to/your/repo --name your-project
```

This installs merge-gate labels, the sandbox hook, a project-specific state directory, and dependencies — then it **halts** (orient → interview → an optional tutorial offer) and exits `0`. It does not seed any Discussions yet. The halt is intentional: the next step needs a human decision, not automation guessing at your backlog.

At the halt, fill in an initial epic backlog under `<your-repo>/epics/epic-<N>-<slug>/epic.md` (goal, why now, scope, what's explicitly out) — the script prints a fill-in-the-blank template when it gets there. Once that exists, re-run with `--resume` to seed it as GitHub Discussions:

```bash
bash scripts/coldstart.sh --path /path/to/your/repo --name your-project --resume
```

`--resume` is idempotent — safe to re-run if a prior seed pass was rate-limited or interrupted partway through.

**One thing coldstart does not verify for you: your target repo needs a `git remote origin` pointing at a real GitHub repo before you run it for real.** Without one, coldstart doesn't fail cleanly — see [Troubleshooting](#troubleshooting) below, it's the first entry. This is also the seam where "does coldstart alone get me to a working team" stops being true: reaching a seeded, running project takes a real GitHub repo and a filled-in backlog, neither of which a script can supply for you. `bash scripts/coldstart.sh --help` prints the full flag list, including `--mode new` for scaffolding a brand-new project from an empty directory instead of wrapping an existing one, and `--self-test` for exercising the halt flow non-interactively with no GitHub calls.

## How the loop works

Once a project is provisioned, "starting the team" means opening **this `fulcrumaxe` checkout** in Claude Code (so it reads `CLAUDE.md` and knows the roles and protocols) and running `/loop` — that's a Claude Code built-in, not something this repo ships a file for. It runs a prompt or slash command on a recurring interval; on its own it self-paces. The cron-driven path to the same thing is `python3 backend/trigger.py "run /loop iteration"`, which is what actually fires each scheduled iteration in production — `scripts/start-the-day.sh` is worth reading if you want the full morning-ritual version of this, but cron itself ships disabled by default and is not the recommended way to start.

**If you provisioned a separate target repo with `--path`, keep `fulcrumaxe` open — not the target.** Running `coldstart.sh` end to end confirms the target repo comes out with only `.autonomous-team/` (state, `project.json`) and its GitHub labels — no `CLAUDE.md`, `scripts/`, or `.claude/agents/` of its own. The Team Lead session, `/loop`, and every spawned role keep running from this `fulcrumaxe` checkout; `--path` and the target's `repo` field just tell those scripts which GitHub repo's Discussions, Issues, and PRs to act on. A separate kit does install a standalone copy of the engine — its own `CLAUDE.md`, `scripts/`, `.claude/agents/`, and a `backend/` snapshot — into a target repo so it can run `/loop` on its own from then on; that's the real "open the target repo instead" path. It's a separate, manually-run step that `coldstart.sh` does not invoke for you, and it lives at `loop-bootstrap/bootstrap.sh`, which ships in the engine repo, the open-source export, and the installed plugin alike (a one-command wrapper, `scripts/coldstart-unified.sh`, chains this same two-step sequence and ships in all three the same way — see [Provisioning: coldstart](#provisioning-coldstart)). See [Updating](#updating) for the exact command.

Every change the team makes starts as a GitHub Discussion and ends as a merged PR:

```
You (or the team) open a Discussion
  → project-manager writes a Spec in the Discussion body
  → executor implements it in an isolated git worktree, opens a PR
  → code-reviewer checks it              → code-review-passed
  → security-reviewer checks it          → security-passed
  → acceptance-tester validates it        → acceptance-passed
  → loop auto-merge, once all three labels are present
```

The three merge-gate labels each verify something different, and a PR needs all of them before the loop will merge it:

- **`code-review-passed`** — a code-reviewer agent read the diff and confirmed it matches the Spec, follows repo conventions, and doesn't introduce obvious bugs.
- **`security-passed`** — a security-reviewer agent checked the change for auth, SQL, secret-handling, or sandbox-rule risk (skipped/auto-passed for changes that don't touch anything sensitive).
- **`acceptance-passed`** — an acceptance-tester agent ran the actual tests/build against the Spec's acceptance criteria and confirmed they pass.

See [CLAUDE.md](CLAUDE.md) ("Merge Gate Protocol") for the enforcement details, and [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor-facing version of this flow.

## Updating

If you installed the standalone kit with `loop-bootstrap/bootstrap.sh` (see [How the loop works](#how-the-loop-works)), re-running it later pulls in fixes and improvements without wiping your customizations — mostly. A fresh clone of the engine gives you the current `loop-bootstrap/bootstrap.sh` — the same way you did the first time (if you installed via the marketplace instead, `/plugin marketplace update fulcrumaxe` refreshes the catalog and updates the installed plugin, including its bundled `loop-bootstrap/bootstrap.sh`, to the latest version on disk):

```bash
git clone https://github.com/fulcrumaxe/fulcrumaxe.git /tmp/fulcrumaxe-engine
bash "/tmp/fulcrumaxe-engine/loop-bootstrap/bootstrap.sh" --repo <owner>/<name> /path/to/your-project
```

It's safe to re-run — nothing here deletes or asks you to confirm anything — but what "safe" means differs by file, and the difference matters:

| What | On re-run |
|---|---|
| `scripts/`, `scripts/lib/`, spawn templates, Python scripts, the `backend/` snapshot | **Overwritten.** This is how fixes and new features reach an already-bootstrapped project. Any local edits to these files are silently lost — don't hand-edit them if you plan to re-run bootstrap later. |
| `.claude/agents/` (the agent role definitions) | **Skipped if the file already exists.** Your customizations survive, but so does staleness: if a role's behavior is improved upstream, a project that bootstrapped before that improvement never receives it through a normal re-run. |
| `CLAUDE.md` | **Skipped if it already exists.** Same trade-off as agent files — your edits survive, upstream changes to the stub don't reach you automatically. |

Verified by actually doing it: bootstrap a throwaway target, hand-edit an installed script and an agent file, re-run bootstrap with no flags, and the script comes back clean while the agent file still has the edit.

**The `--force` flag is all-or-nothing.** Passing `--force` makes `.claude/agents/` behave like `scripts/` — every agent file gets overwritten unconditionally, including ones you've customized. There's no per-file selection; it's everything or nothing. If you've hand-tuned even one role's prompt, `--force` throws that tuning away along with every other agent file's customizations. Back up `.claude/agents/` first if that matters to you.

**Bottom line if you want a role definition to actually update:** delete that one file from `.claude/agents/` before re-running bootstrap (so the "skip if present" branch doesn't trigger for it), or accept the `--force` trade-off and re-apply your customizations afterward. There's no partial-update path today.

### AF_ROOT

The standalone kit's `scripts/start-dashboard.sh` doesn't ship the dashboard itself — `backend/` and `dashboard/` live in a `fulcrumaxe` checkout, not in your bootstrapped project. That script delegates to a real `fulcrumaxe` checkout via the `AF_ROOT` environment variable, which you have to set — there's no way for it to guess where you cloned `fulcrumaxe` to, and it refuses to run rather than delegate to the wrong tree:

```bash
AF_ROOT=/path/to/your/fulcrumaxe/checkout bash scripts/start-dashboard.sh
```

Set it once in your shell profile if you'll be running this more than a few times.

## Dashboard

The dashboard is what an operator actually watches while the team works — a live agent feed, loop controls, budget and health checks, an open-PR inspector with gate-label state, and a fleet view across every project the team runs in.

```bash
bash scripts/start-dashboard.sh
```

This starts all four services it needs (REST API, JSON-RPC adapter, SSE bridge, and the Vite frontend) and prints the URL it lands on once everything's up — `http://localhost:5173` with default ports. Stop everything with `bash scripts/stop-dashboard.sh`. Ports are configurable (`AF_API_PORT` / `AF_RPC_PORT` / `AF_SSE_PORT` / `AF_VITE_PORT`, or a `ports` block in the target repo's `project.json`, under `.autonomous-team/`) if the defaults collide with something already running. See [dashboard/README.md](dashboard/README.md) for the JSON-RPC method reference and how the pieces fit together.

## Agent roles

Every role's spawn contract, gates, and review protocol lives in its own file rather than one growing monolith:

```bash
ls .claude/agents/
```

At the time of writing that's 26 files — executor, code-reviewer, security-reviewer, project-manager, acceptance-tester, and roles for browser testing, architecture review, cost/performance/security perspectives, and more. Don't trust a number pasted here to stay current; the command above always reflects what's actually in the tree. `CLAUDE.md` is where Team Lead orchestration and the per-role protocols are documented.

The team runs on Claude via the Agent SDK — Team Lead spawns a role per Discussion, each role works in an isolated git worktree sandboxed from `main` until its PR is reviewed and merged.

## Custom agent roles (plugins)

You don't need to fork this repo to add a role specific to your own project. Drop a YAML manifest under `.autonomous-team/plugins/` and it's loaded as an extra agent role alongside the built-ins — no code change required.

```bash
mkdir -p .autonomous-team/plugins
cat > .autonomous-team/plugins/changelog-writer.yaml <<'EOF'
name: changelog-writer
description: "Writes a CHANGELOG.md entry for each merged PR"
system_prompt: |
  You are a changelog specialist for this project. Given a merged PR's
  title and diff, add one concise entry to CHANGELOG.md describing the
  change for a human reader. Do not edit any other file.
version: "1.0"
tools:
  - Read
  - Edit
review_pipeline: code-only
triggers:
  - on: pr_label
    value: needs-changelog
EOF
```

`name`, `description`, and `system_prompt` are required. Everything else has a default the loader fills in if you leave it out:

- `version` — free-form string, defaults to `"1.0"`.
- `tools` — restricts which tools the role can call. Omit it and the role gets every tool. The loader stores whatever strings you put here — it doesn't check them against Claude Code's real tool names, so there's no fixed vocabulary to copy from this doc. Use Claude Code's actual tool names (`Read`, `Edit`, `Bash`, and so on).
- `review_pipeline` — one of `code-only`, `code+security`, `none`. Defaults to `code-only`.
- `triggers` — a list of auto-spawn conditions the loader records as-is; this doc isn't claiming anything currently acts on them.

A few rules the loader enforces:

- The file must live directly in `.autonomous-team/plugins/` and its name must end in `.yaml` — `.yml` is not picked up.
- `name` must match `[a-z][a-z0-9-]*` and can't be one of the six reserved built-in names (`executor`, `code-reviewer`, `security-reviewer`, `project-manager`, `acceptance-tester`, `team-lead`).
- A manifest that fails validation — bad name, a missing required field, an unrecognized `review_pipeline` — is skipped with a warning logged at startup, and the rest of the team keeps running normally. If a role you added doesn't show up, that log is the first place to look.

Once loaded, the role shows up in the dashboard's REST API — `GET /plugins` and `GET /plugins/{name}` — merged into the same role list the built-ins appear in. Those endpoints require the same bearer auth and RBAC check as the rest of the API, so a bare unauthenticated `curl` won't return anything. Plugin roles aren't currently exposed over the GraphQL API.

## Configuration and state

Runtime state — the blackboard, session data, metrics, the audit trail — lives **outside the repo**, so a `git worktree` merge can never wipe it. `backend/state_paths.py` is the single source of truth for where; the short version is `$AUTONOMOUS_TEAM_STATE_DIR` if you've set it, otherwise a per-project default derived from the project name (`coldstart.sh` sets this up for you), falling back to `~/.fulcrumaxe-state/` if nothing else applies.

Two files control project behavior, both under `.autonomous-team/` in the target repo:

- **`project.json`** — created and merged (never overwritten) by `coldstart.sh`: state dir, ports, language, branch/commit patterns.
- **`config.json`** — the feature gates (`gates.auto_merge`, `gates.security_review`, `gates.allow_claude_spawn`, and others). **`coldstart.sh` does not create this file.** If it's missing, the dashboard's backend refuses to start until you add it — see the second [Troubleshooting](#troubleshooting) entry.

Separately, an **autonomy dial system** governs how much the team is allowed to do unattended — agent spawning, merges, writes to external systems like GitHub, docs, memory, archiving — independent of the two files above. Run `python3 backend/dial_registry.py list` to see every class and its current level.

- Each class sits on its own **1–5 scale**, independently: 1 is the most restricted, 5 the most autonomous.
- **Changing a level requires an allowlist entry first.** Every mutation path — CLI and dashboard tile alike — calls `set_dial()` with a `source` descriptor that must already be listed in `<STATE_DIR>/dial-directive-allowlist.json`, or it's refused. The file is created empty (`[]`) by the first `set_dial()` call — `coldstart.sh` seeds it with a working dashboard entry (and your operator login, when one resolves) via `scripts/provision-dial-allowlist.sh`, so a coldstarted install already has one authorized source. Run that script by hand — it's idempotent — to (re-)provision it, or to add your own entry to an install that predates this.
- An entry is a JSON object matching the shape of the `source` the caller passes. Add the matching entry and either route works the same way — neither is more "practical" than the other:

  ```json
  // allowlists: dial_registry.py set ... --source '{"kind":"system","reason":"cli"}'
  {"kind": "system", "reason": "cli"}

  // allowlists the dashboard's Dial Controls tile (its source is fixed to this shape)
  {"kind": "system", "reason": "dashboard_rpc"}
  ```

- Some classes have a hardcoded **ceiling** below 5 that no directive can raise: `sandbox.modify` ships with ceiling 1, meaning it can never be turned up at all.

## Architecture at a glance

```
GitHub
 ├── Discussions — ideas, specs, coordination
 ├── PRs — implementation, review, merge
 └── Issues — team log, tracking

Team Lead (this session)
 └── spawns agent roles per Discussion (executor, code-reviewer, ...)
      └── Agent SDK session → Claude
           └── each role works in an isolated git worktree, sandboxed
               from main until its PR is reviewed and merged

dashboard/   — browser-based operations console (Python + Vite/React)
              live agent event feed, loop controls, project status
ts-backend/  — Bun + Hono TypeScript backend, additive parity port that
              runs alongside the Python one rather than replacing it
backend/     — Python backend: server, loop orchestration, state, stats
```

## Troubleshooting

The first three things that go wrong for a new operator, and what to do about them:

- **`coldstart.sh` exits with code `2` right after printing `=== coldstart.sh: labels ===`, with no error message.** Cause: the `--path` target has no `git remote origin`. The labels step resolves the repo slug from `origin` to bootstrap merge-gate labels; when that lookup fails, the failure propagates through the pipeline before the script's own "no origin remote, skipping" warning ever gets a chance to print. Fix: add a real GitHub `origin` remote to your target repo before running coldstart for real — `git -C /path/to/your/repo remote add origin git@github.com:<owner>/<repo>.git`. You'll need one anyway; Discussions and PRs have to live somewhere.
- **The dashboard's backend refuses to start** — `start-dashboard.sh` reports `backend/api.py did not respond on port ... within 30s`, and the api.log in the target repo's dashboard-logs (under `.autonomous-team/`) says the spawn gate — `gates.allow_claude_spawn` — is missing from config.json. Cause: `coldstart.sh` wires `project.json` but does not create `config.json`, and the spawn gate hard-fails closed rather than silently allowing spawns. Fix: create a `config.json` next to `project.json`, in the target repo with at least `{"gates": {"allow_claude_spawn": true}}`, then re-run `start-dashboard.sh`.
- **A prompt-lane request comes back with `` "error": "no SDK credential — set CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or run `claude login`" `` .** Cause: none of the three credential sources from [Prerequisites](#prerequisites) is present in the environment the backend is running in. Fix: set one of the two env vars, or run `claude login` once before starting the dashboard or the loop.

## Contributing

Open a GitHub Discussion describing what you want and why — no code required. See [CONTRIBUTING.md](CONTRIBUTING.md) for how work flows from idea to merged PR.

## License

Licensed under AGPLv3 — see [LICENSE](LICENSE).

Copyright (C) 2026 Formal Hosting LLC and fulcrumaxe contributors.
