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

This runbook describes that gesture. Performing the steps below is
necessary for the verifier to move off `UNCONTAINED` — it is not
sufficient by itself. Reaching `CONTAINED` also requires a separate
network-denial decision this runbook does not make; see "Verifying it
worked" below.

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
- **network** — whether the process can reach the public internet. Nothing
  in the numbered gesture below changes this; see "Verifying it worked" for
  what that means for the verdict.

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
   not to `gate1-invoke.sh` itself, and not to any `bash` binary.**
   `gate1-invoke.sh` does not sudo itself; it execs:
   ```
   sudo -u "$GATE1_RUNNER_UID" --preserve-env=RUN_PR_TESTS_TREE_ROOT "$RUNNER" "$PR_NUMBER"
   ```
   The command sudo is asked to authorise is `<operator-checkout>/scripts/run-pr-tests.sh <PR_NUMBER>`
   — a `sudoers.d` rule written against `.../gate1-invoke.sh` will never
   match this, sudo will refuse, and the predictable 2am repair is to widen
   the rule until it works. **Widening the rule defeats the entire point of
   scoping it — fix the exact path in the rule instead of loosening it.**
   Write the literal command, e.g.:
   ```
   # /etc/sudoers.d/gate1-runner (adjust the path to this checkout)
   %gate1_callers ALL=(gate-runner) NOPASSWD: /home/OPERATOR/CHECKOUT/scripts/run-pr-tests.sh *
   Defaults:%gate1_callers env_keep += "RUN_PR_TESTS_TREE_ROOT"
   ```
   Three things this recipe is deliberately careful about — do not "fix" any
   of them away under time pressure:
   - **Do not name a `bash` binary in the `Cmnd`, and do not invoke the
     runner through one.** `run-pr-tests.sh` is mode `755` with its own
     `#!/usr/bin/env bash` shebang, so `gate1-invoke.sh` execs the script's
     own path directly rather than `bash <script>` — and the rule above
     names only that same script path. Naming a specific `bash` binary
     instead (e.g. `/run/current-system/sw/bin/bash`) binds the rule to
     wherever that symlink happens to resolve *today*; it can point to a
     different store path than the one the process invoking `sudo` resolves
     `bash` to (interactive shell vs. review-lane spawn environment, or
     simply after the next `nixpkgs` rebuild), and when the two disagree
     sudo refuses — again inviting the same "just widen it" repair. Pinning
     the script's own path instead of an interpreter removes that seam
     entirely: the script's path inside the checkout doesn't move on a
     rebuild the way a `/run/current-system/...` symlink does.
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

4. **Grant the runner uid read and traverse access to the checkout — and
   only that.** The `state-dir` and `operator-checkout-write` probes in
   `gate1-verify-containment.sh` cannot run at all unless the runner uid can
   first reach the files they inspect. That requires exactly three things,
   together:
   - **(a) `x` (traverse) permission on every ancestor directory of the
     checkout** — most immediately the operator's own home directory.
   - **(b) read on the checkout tree, and execute on
     `scripts/run-pr-tests.sh`** — the file Gate 1 exists to run.
   - **(c) no write anywhere under the checkout, `.git` included, and no
     read on `$AUTONOMOUS_TEAM_STATE_DIR`** — the two things this gesture
     exists to deny.

   **The collision this step exists to name:** a `0700` operator home
   denies (a). And (a) is the *precondition* the `state-dir` and
   `operator-checkout-write` probes need before they can run at all —
   without it, both print `INDETERMINATE`, which is not the same reading as
   `DENIED`. Granting (a) and (b) without disturbing (c) is the actual
   problem this step solves; nothing else in this gesture (user creation,
   the sudo rule, `GATE1_RUNNER_UID`) touches it.

   **Three ways to grant (a) and (b) while holding (c) — priced, not
   chosen. This repo does not pick one: each trades differently against
   whatever else `0700` is protecting on this host, and that tradeoff
   belongs to the operator who owns the host.**

   - **ACL on the checkout path.** `setfacl` granting the runner uid `r-x`
     on the home directory and read+execute through the checkout, leaving
     `0700` intact everywhere else on the home. Narrowest change. Gives up:
     an access grant `ls -l` does not show — easy to forget is there, and
     easy to lose silently on a restore that does not preserve extended
     attributes.
   - **`/home/OPERATOR` to `0711` with a group-readable checkout.** Standard
     permission bits, visible in `ls -l`. Gives up: the posture of the
     *entire* home directory changes, not just the one path the runner
     needs — every other directory under it becomes traversable too (each
     still gated by its own mode, but the ancestor block is gone for all of
     them, not just this checkout).
     **The trap measured on this host:** the obvious way to make the
     checkout group-readable is to add the runner uid to the checkout's
     owning group. `/home/jp/fulcrumaxe/.git` is `0775 jp:users` —
     **group-writable**. Adding the runner to `users` grants it group
     *write* on `.git`, which flips `operator-checkout-write` from `DENIED`
     to `NOT-DENIED` — the step meant to enable containment would silently
     defeat it. If this shape is chosen, the runner needs a group that has
     read on the checkout without inheriting `users`' write bit on `.git` —
     a dedicated group, or an ACL entry scoped to read only, not plain
     membership in the checkout's existing owning group.
   - **Relocate the checkout outside the operator's home entirely.**
     Cleanest boundary — there is no ancestor-directory problem left to
     solve. Gives up: the most disruption of the three, since every path
     the operator, CI, and every other tool on this host uses to reach the
     checkout changes.

   Apply whichever shape the operator picks before continuing. This
   runbook does not recommend one.

5. **Point `GATE1_RUNNER_UID` at the new user.** `scripts/gate1-invoke.sh`
   reads this from the process environment: when set, it runs the suite
   via `sudo -u "$GATE1_RUNNER_UID"` and, if that user does not exist,
   refuses to run anything at all rather than silently falling back to
   same-uid. Set it in whatever environment invokes Gate 1 (the review
   lane's spawn environment, or an operator's own shell for a manual run).

6. **Re-run the verifier as the new user, with both targets pinned to their
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
   `/home/OPERATOR/...` placeholders above.) With only step 4's access fix
   applied — no network-denial decision made — expect exactly three probes
   to read `DENIED` (`gh-credential`, `state-dir`, `operator-checkout-write`)
   and `network` to still read `NOT-DENIED`, because nothing in this
   gesture touches the network; the verdict at that point is
   `UNCONTAINED`, not `CONTAINED`. That is the correct and expected outcome
   of this gesture alone — see "Verifying it worked" below for why
   `CONTAINED` needs one more decision this runbook does not make. If
   instead you see `INDETERMINATE` on any line, that probe never actually
   ran — fix whichever precondition it names (a missing `gh`/`curl` binary
   as the new user, or one of the two paths above not pointing at the real
   operator location) and re-run; do not treat `INDETERMINATE` as good
   enough, and do not mistake three `DENIED` plus `UNCONTAINED` for
   `INDETERMINATE` either — they are different states with different
   causes. Paste that output into the tracking Discussion so the record
   shows exactly which containment state is live.

## Do not automate this

This gesture requires editing the host's NixOS configuration, `sudoers`,
and running `chmod`/an access-control change against paths outside the
repository. None of it runs in CI, and none of it is something an executor,
reviewer, or any other automated role should attempt:

- **No executor may create a system user, edit NixOS configuration, edit
  `sudoers`, or change permissions on `~/.autonomous-forever-state` or the
  operator's home directory.** These are host changes with no automated
  rollback and no sandboxing — exactly the class of action that needs a
  human to read this runbook and decide, not an agent executing it
  unattended. That includes picking one of the three access shapes in step
  4 above — the choice is the operator's, not an executor's, and not this
  runbook's.
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
`UNCONTAINED`. After the access fix in step 4 above alone (run as step 6,
with both paths pinned): three probes — `gh-credential`, `state-dir`,
`operator-checkout-write` — read `DENIED`, and `network` still reads
`NOT-DENIED`, because nothing in the numbered gesture above denies the
network. The verdict at that point is `UNCONTAINED`, not `CONTAINED` —
reaching `CONTAINED` requires all four `DENIED`, and this runbook does not
include a network-denial step. Getting there needs a further, separate
decision that denies the network at this uid; this runbook does not name
or choose one. Until that decision is made and applied, three `DENIED`
plus `network=NOT-DENIED` plus `UNCONTAINED` is the correct and expected
result of the access fix alone — it is not a sign the gesture failed. A
verdict of `INDETERMINATE` (forced whenever any single probe is
`INDETERMINATE`, by design — an unrun probe must never look like a passing
one) means the run itself is inconclusive, not that containment succeeded
or failed. The script's exit code is always `0` regardless of verdict — it
reports, it does not gate — so read the printed lines, not the exit
status.
