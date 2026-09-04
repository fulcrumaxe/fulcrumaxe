# Contributing

Thanks for looking at this project. This doc covers the mechanics of getting
a change in: how to submit it, what happens after you open a pull request,
and what to expect back.

## Before you start

For anything bigger than a small fix, it's worth opening a GitHub Discussion
first to talk through the approach — saves you writing code that ends up
going in a different direction. For bug fixes and small improvements, just
open a PR.

## The way this project is actually built

Most of what lands here didn't start as a hand-written diff — it started
with fulcrumaxe running against its own repo. You can do the same thing on
your fork: clone it, point fulcrumaxe at it using your own Claude
subscription or API key, let the project-manager role turn your idea into a
frozen Spec, and let the executor role implement it. Then open a PR that
carries both the Spec and the diff.

A few reasons this is worth trying instead of writing a PR by hand:

- **It runs on your compute, not the maintainer's.** Nobody else's agent
  run shows up on someone else's bill.
- **It's the best way to find out whether the tool is worth using.** You
  end up a contributor and a user in the same motion.
- **The Spec is a faster thing to review than the diff.** A reviewer can
  check that the intent makes sense before working through whether the code
  matches it — the same two-step review this project's own team runs on
  itself.
- **It keeps outside input out of the maintainer's own loop.** Nothing from
  this repo's public Discussions or Issues feeds into the maintainer's
  agents — your run happens entirely on your fork, under your own account.

None of this is required. Hand-written PRs are just as welcome — if you're
fixing a typo or a one-line bug, forking, branching, and writing the diff
yourself is still the simplest way to do it.

## How to submit a change

1. **Fork the repo** and clone your fork locally.
2. **Create a feature branch** off `main` — `git checkout -b fix-thing-that-was-broken`.
3. **Make your change** and commit it. Write commit messages that explain
   *why*, not just *what*.
4. **Push to your fork** and **open a pull request against `main`**.

That's the whole flow. No special tooling required on your end — this
works the same whether your change came from fulcrumaxe or from your own
editor.

## What happens after you open a PR

**CI runs automatically.** GitHub Actions lints, typechecks, builds, and
tests every workspace touched by your change (`.github/workflows/ci.yml`).
This runs on every PR, including ones from forks — it needs to pass before
anyone looks at your diff seriously.

**A maintainer reviews once CI is green.** There's no bot that merges things
automatically for external PRs. A human reads your diff and either approves,
asks for changes, or explains why it's not a fit.

**Security review is mandatory for external contributions.** Because this
project runs autonomous agents against its own repo, any PR that traces back
to a contribution from outside the maintainer group gets a security review
before it's acted on further, and requires explicit maintainer approval
before that work moves forward — regardless of how small the change looks.
This isn't a judgment on you personally; it's a blanket rule for anything
that didn't originate from inside the maintainer group, applied consistently
to every outside contribution. In practice this means your PR may sit for a
bit while that review happens — that's expected, not a sign it's being
ignored.

## If your PR needs changes or gets rejected

You'll get a comment explaining the specific reason — not just a label or a
silent close. If it needs changes, the comment will say what to change. If
it's rejected outright, the comment will say why (out of scope, duplicates
existing work, conflicts with the project's direction, etc.).

This is a solo-maintained project, so there's no promised turnaround —
you'll get best effort, not a guaranteed response window. PRs that fail CI
won't get a substantive review until they pass. If it's been a while and
you haven't heard anything, feel free to comment on your own PR as a nudge.

## Code style

Match the conventions already in the file you're editing. If the project has
a linter or formatter configured for that workspace, CI will catch style
issues — run it locally first (`npm run lint`, `npm run typecheck`, etc.,
depending on which workspace you're in) to save yourself a round trip.

## Tests

If you're fixing a bug, add a test that would have caught it. If you're
adding a feature, add tests that cover the new behavior. PRs that add code
without any test coverage are more likely to come back with review comments.

## Where design discussions happen

Bigger proposals and design questions live in this repo's GitHub Discussions
tab. It's a good place to check before starting on something non-trivial, and
a good place to ask if you're not sure whether an idea would be welcome.

Worth being clear about what this is, though: it's a conversation with a
human, not a work queue. No autonomous team watches this repo, so nothing
posted here gets picked up automatically on a loop iteration. If you want
something built, the fork-and-run path above is how you make that happen
yourself.

## License and copyright

Contributions are licensed under this project's AGPLv3, same as everything
else in the repo. There's no CLA — you keep the copyright on the code you
write, and opening a PR here means you're licensing that contribution under
AGPLv3 alongside the rest of the codebase.
