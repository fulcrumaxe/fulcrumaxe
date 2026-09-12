# Gate 1 Containment Runbook

**Audience:** the human operator. **Not** an executor, reviewer, or any
other automated role — see "Do not automate this" below.

## What this runbook is for

`scripts/gate1-invoke.sh` and `scripts/gate1-verify-containment.sh` separate
which *copy* of `scripts/run-pr-tests.sh` executes from which *tree* it runs
the test suites against. That separation is necessary for containment, but
it is not sufficient by itself: today, run at the same uid as everything
else on this host, `scripts/gate1-verify-containment.sh` reports all four
of the capabilities below as `NOT-DENIED` and an overall verdict of
`UNCONTAINED`. Nothing in the repo can close that gap — it requires an
actual host-level identity boundary, performed once by an operator on the
machine itself, outside the repo and outside CI.

This runbook describes that gesture. Performing it is what moves the
verifier's verdict from `UNCONTAINED` to `CONTAINED`.

## The four capabilities

`scripts/gate1-verify-containment.sh` probes exactly these, none of which a
Gate-1 pass/fail signal actually needs:

- **gh-credential** — whether the process can obtain a working GitHub
  token via `gh auth token`.
- **state-dir** — whether the process can READ
  `$AUTONOMOUS_TEAM_STATE_DIR` (default `~/.autonomous-forever-state`). This
  probes read, not write, because the risk step 3 below closes is read: a
  second uid on this host can already read `audit.jsonl`, `state.db`, and
  `discussion_cache.db` while the directory stays `0755`. A write-only probe
  would flip to `DENIED` the moment write permission is removed even if the
  directory is still fully readable — reporting on a capability nobody was
  worried about.
- **operator-checkout-write** — whether the process can write into the
  operator's own checkout, `.git` included.
- **network** — whether the process can reach the public internet.

## Why environment scrubbing does not help

An earlier round of thinking assumed the credential lived in
`~/.config/gh/hosts.yml` and that clearing `HOME`, `GH_TOKEN`, or
`GITHUB_TOKEN` before a test run would deny it. Measured on this host: it
does not. The credential is stored in the system keyring, not in that file,
and `gh auth token` still succeeds with `HOME` pointed at an empty scratch
directory and both env vars unset. Any same-uid environment change is
worthless against a credential that isn't read from the environment or
`$HOME` in the first place. The only thing that denies any of the four
capabilities above is a different uid.

## The gesture (uid separation)

1. **Create a dedicated, unprivileged system user** for running Gate-1
   suites — a NixOS declarative user (`users.users.<name>` in the host's
   NixOS configuration), not an ad-hoc `useradd`, so the account survives a
   rebuild and stays auditable in version control on the host.
   - No login shell needed beyond what's required to run test suites.
   - Not a member of the `docker` group or any other group that is
     root-equivalent on this host.
   - Its own home directory, separate from the operator's.

2. **Scope a `sudo` rule to the command `gate1-invoke.sh` actually runs —
   not to `gate1-invoke.sh` itself.** `gate1-invoke.sh` does not sudo
   itself; it execs:
   ```
   sudo -u "$GATE1_RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT bash "$RUNNER" "$PR_NUMBER"
   ```
   The command sudo is asked to authorise is `bash <operator-checkout>/scripts/run-pr-tests.sh <PR_NUMBER>`
   — a `sudoers.d` rule written against `.../gate1-invoke.sh` will never
   match this, sudo will refuse, and the predictable 2am repair is to widen
   the rule until it works. **Widening the rule defeats the entire point of
   scoping it — fix the exact path in the rule instead of loosening it.**
   Write the literal command, e.g.:
   ```
   # /etc/sudoers.d/gate1-runner (adjust the path to this checkout)
   %gate1_callers ALL=(gate-runner) NOPASSWD: /run/current-system/sw/bin/bash /home/OPERATOR/CHECKOUT/scripts/run-pr-tests.sh *
   Defaults:%gate1_callers env_keep += "RUN_PR_TESTS_TREE_ROOT"
   ```
   Two things this recipe is deliberately careful about — do not "fix" either
   away under time pressure:
   - **Never put `/usr/bin/env` (or any `env` invocation) in the `Cmnd`.** A
     rule that permits `env` with arbitrary arguments permits arbitrary
     execution as the target user — `env` will run anything it's told to.
     The invocation above avoids this on purpose: the tree root reaches the
     runner via sudo's own `--preserve-env`, not via wrapping the command in
     `env VAR=val ...`, so the authorised `Cmnd` is the runner script itself.
   - **Leave `env_reset` on, and keep `env_keep` to exactly this one
     variable.** Sudo defaults to `env_reset` (wiping the calling
     environment) already; the only addition here is `RUN_PR_TESTS_TREE_ROOT`.
     Widening `env_keep` to include `GH_TOKEN` or `GITHUB_TOKEN` — or turning
     `env_reset` off — hands the operator's own credential straight through
     to the "contained" uid, undoing this gesture's entire point through the
     one path environment scrubbing actually matters for (see "Why
     environment scrubbing does not help" above — the keyring credential
     doesn't need env vars at all, but a sudoers mistake that leaks
     `GH_TOKEN`/`GITHUB_TOKEN` reopens a *different*, real hole).

3. **Tighten the state directory.** `~/.autonomous-forever-state` is
   `0755` today — world-readable, so a second uid can already read
   `audit.jsonl`, `state.db`, and `discussion_cache.db` even before this
   gesture is performed. Once the new user exists, `chmod 0700` that
   directory (owned by the operator's existing user) so the new uid can no
   longer read it. Doing this before the new uid exists provides no benefit
   and is not part of this step — it is bound to uid creation deliberately,
   so there is never a window where the boundary exists but the directory
   is still open.

4. **Point `GATE1_RUNNER_UID` at the new user.** `scripts/gate1-invoke.sh`
   reads this from the process environment: when set, it runs the suite
   via `sudo -u "$GATE1_RUNNER_UID"` and, if that user does not exist,
   refuses to run anything at all rather than silently falling back to
   same-uid. Set it in whatever environment invokes Gate 1 (the review
   lane's spawn environment, or an operator's own shell for a manual run).

5. **Re-run the verifier as the new user, with both targets pinned to their
   real absolute operator-side paths.** Running as a different uid changes
   `$HOME`, and the state-dir probe's default resolves against `$HOME` — so
   an unpinned run under the new user's own home checks a directory that
   was never the real one, and (correctly, since that path doesn't exist)
   reports `INDETERMINATE`, not the `DENIED` you're trying to confirm.
   Always pass both explicitly:
   ```bash
   sudo -u <new-user> env \
     GATE1_VERIFY_STATE_DIR=/home/OPERATOR/.autonomous-forever-state \
     GATE1_VERIFY_CHECKOUT_DIR=/home/OPERATOR/CHECKOUT \
     bash /home/OPERATOR/CHECKOUT/scripts/gate1-verify-containment.sh
   ```
   (Substitute this host's real operator home and checkout path for the
   `/home/OPERATOR/...` placeholders above.) Confirm all four capabilities read
   `DENIED` and the verdict reads `CONTAINED`. If instead you see
   `INDETERMINATE` on any line, that probe never actually ran — fix
   whichever precondition it names (a missing `gh`/`curl` binary as the new
   user, or one of the two paths above not pointing at the real operator
   location) and re-run; do **not** treat `INDETERMINATE` as good enough.
   Only a run with zero `INDETERMINATE` lines and all four `DENIED` is
   evidence containment is live. Paste that output into the tracking
   Discussion so the record shows containment is live, not merely built.

## Do not automate this

This gesture requires editing the host's NixOS configuration, `sudoers`,
and running `chmod` against a directory outside the repository. None of it
runs in CI, and none of it is something an executor, reviewer, or any other
automated role should attempt:

- **No executor may create a system user, edit NixOS configuration, edit
  `sudoers`, or `chmod` `~/.autonomous-forever-state`.** These are host
  changes with no automated rollback and no sandboxing — exactly the class
  of action that needs a human to read this runbook and decide, not an
  agent executing it unattended.
- **This is a host-provisioning gesture requiring boss approval**, not a
  merge in this repository. There is nothing to review as a PR — the
  before/after state lives on the host, not in git history.

## Verifying it worked

```bash
bash scripts/gate1-verify-containment.sh
```

Each probe line reads one of three states, not two — this matters for
reading the result correctly:
- `NOT-DENIED` — the probe ran and the capability was reachable.
- `DENIED` — the probe ran and the capability was NOT reachable.
- `INDETERMINATE` — the probe's own precondition failed (a missing `gh` or
  `curl` binary, or one of `GATE1_VERIFY_STATE_DIR`/`GATE1_VERIFY_CHECKOUT_DIR`
  not pointing at a real, existing target) — it never got to test anything.
  This is not a third kind of pass. It means re-run with whatever it's
  missing fixed; it must never be read as, or mistaken for, `DENIED`.

Before this gesture: all four probes read `NOT-DENIED`, verdict
`UNCONTAINED`. After (run as step 5 above, with both paths pinned): all
four should read `DENIED`, verdict `CONTAINED`. A verdict of
`INDETERMINATE` (forced whenever any single probe is `INDETERMINATE`, by
design — an unrun probe must never look like a passing one) means the run
itself is inconclusive, not that containment succeeded or failed. The
script's exit code is always `0` regardless of verdict — it reports, it
does not gate — so read the printed lines, not the exit status.
