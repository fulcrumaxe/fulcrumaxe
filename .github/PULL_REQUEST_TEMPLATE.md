## What does this change do?

<!-- A short summary — what changed and why. -->

## Linked issue / discussion (if any)

<!-- e.g. "Fixes #123" or a link to a GitHub Discussion. Leave blank if none. -->

## How I tested this

<!-- What you ran, and what you saw. Manual steps are fine — just be specific. -->

<!-- Optional: if a test in this PR is new or changed and you want CI to
reproduce that it can actually fail (not just claim it), uncomment this and
fill it in. The pr-mutation-evidence check applies the diff, runs the
command on the clean tree and again on the patched tree, and requires green
then red. Leave it commented out (or delete it) if you have nothing to
declare — an absent block is never treated as a failure.

## Mutation evidence

Host shape: fresh clone (CI runner) | linked worktree | both
Command: pytest tests/test_foo.py::test_bar -q

```diff
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -N,M +N,M @@
-    old line
+    new line
```
-->

## Frozen Spec (if this came from fulcrumaxe)

<!-- If this PR was produced by running fulcrumaxe against your fork, paste
the frozen Spec here (or link to the Discussion on your fork that holds it).
Reviewing the Spec first makes the diff much faster to check. If you wrote
this PR by hand, skip this section entirely — it doesn't apply to you. -->

## Checklist

- [ ] Tests added or updated for this change
- [ ] CI is expected to pass (lint, typecheck, build, tests)
