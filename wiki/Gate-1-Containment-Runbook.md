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
- **state-dir** — whether the process can write into
  `$AUTONOMOUS_TEAM_STATE_DIR` (default `~/.autonomous-forever-state`).
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

2. **Scope a `sudo` rule to the wrapper only.** The new user must be able to
   invoke `scripts/gate1-invoke.sh` (and nothing else) as itself, and the
   operator must be able to `sudo -u <new-user>` that one command — not an
   unrestricted shell, not arbitrary commands. A `sudoers.d` drop-in
   restricted to the exact wrapper path is the shape to aim for; a bare
   `ALL=(ALL) NOPASSWD: ALL` entry defeats the entire point.

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

5. **Re-run the verifier as the new user** (or point Gate 1 at it via step
   4) and confirm `scripts/gate1-verify-containment.sh` now reports all
   four capabilities `DENIED` and the verdict `CONTAINED`. Paste that
   output into the tracking Discussion so the record shows containment is
   live, not merely built.

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

Before this gesture: all four probes read `NOT-DENIED`, verdict
`UNCONTAINED`. After: all four should read `DENIED`, verdict `CONTAINED`.
The script's exit code is always `0` either way — it reports, it does not
gate — so read the printed lines, not the exit status.
