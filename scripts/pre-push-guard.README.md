# Local pre-push guard

`scripts/install-pre-push-guard.sh` writes a `pre-push` hook into the checkout
you run it from. The hook refuses **one** push shape: an update to the remote
`main` that is not a fast-forward.

```bash
bash scripts/install-pre-push-guard.sh
```

Run it once per clone. It prints `installed`, `already installed`, or
`updated`, and exits `0` in all three cases; running it twice leaves exactly
one hook file, never two.

## What it refuses

Only when the ref being written on the remote is `refs/heads/main`:

- a **non-fast-forward** update — the remote's current commit is not an
  ancestor of what you are pushing, so the push rewinds or rewrites history
- a **deletion** of `main`

## What it does not touch

- fast-forward pushes to `main`, including ones spelled with `--force`
- every push to every other branch and tag, force or not
- anything at all when the remote does not yet have `main`
- anything at all when it cannot reach a verdict — if the remote's commit is
  not present locally (a stale or never-fetched remote), it prints one line
  saying so and allows the push

That last bullet is the design, not a gap. This guard exists to catch an
accident, and the cost of a false positive is higher than the cost of a miss:
a hook that blocks legitimate work gets deleted by hand within a week, and
then it is guarding nothing. Every branch that cannot decide allows.

## Bypassing it

```bash
git push --no-verify --force origin main
```

`--no-verify` skips the hook entirely, so that command still succeeds. This is
stated here rather than hidden because the bypass is not a defect — it is what
makes this a guardrail rather than a boundary, and an operator who needs to
rewind `main` on purpose needs a way to do it. The refusal message prints the
same instruction.

Other things that get past it, for the same reason: any clone where the
installer was never run (the hook lives in `.git/`, which is not tracked, so it
does not travel with the repository), and any push made with `core.hooksPath`
pointed elsewhere.

## What it is not

It is not a security boundary and was not built to resist someone who wants to
get around it. Score changes to it on accidental-write risk, not on
adversarial-bypass risk — the same rule `CLAUDE.md` sets for `hooks/`.

Server-side branch protection would be the real control. It is not available:
`branches/main/protection` and `rulesets` both return `403` on this
repository's plan. This hook is the only push-side guard that can actually be
installed, so it is what exists.

## Removing it

Delete `.git/hooks/pre-push`. Nothing else records that it was installed.

## Notes on the installer

- It refuses to overwrite a `pre-push` hook it did not write, and exits `1`
  saying so. Move the existing hook aside first if you want this one.
- It refuses to install when `core.hooksPath` is set, and exits `1` saying so.
  Git would ignore `.git/hooks/pre-push` in that configuration, and a hook that
  silently never runs is worse than no hook — the operator believes they are
  covered.
- It writes through a temporary file in the hooks directory, so an interrupted
  run cannot leave a half-written hook behind.
