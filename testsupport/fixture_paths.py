"""Synthetic filesystem paths shared by the path-classification test suites.

**This is not a resolver.** It holds constants, nothing else — it never reads
the environment, never shells out to git, and never answers "where is this
checkout". backend/repo_root.py is the one module that answers that question
(scripts/lib/repo-root-resolve.sh is its shell twin); adding a second one is a
D#1997 failure condition. What lives here is the opposite kind of value: paths
that deliberately correspond to nothing on any real machine.

Why it exists: roughly sixty fixture lines across twenty test files each spelled
out their own synthetic checkout root, and every one of them picked the same
literal — a real operator's home directory. tests/conftest.py already said so in
prose ("they were never asserting anything about this machine's real path, just
checking the tiering logic against made-up paths"), but prose does not stop the
next test file from copying the literal, and it did not stop the checkout-path
guard from having to allowlist all sixty.

The root is `/synthetic/...` on purpose, rather than a home-shaped path. These
suites classify by path *shape* — "is this cwd a worktree, the main checkout, or
somewhere untrusted" — and nothing in hooks/sandbox_rules.py keys on /home/, so
a home-shaped root buys no fidelity while costing the one thing that matters
here: nobody can mistake `/synthetic/main-repo` for a real checkout. Mistaking
exactly that is what produced the sixty-entry misclassification these constants
replace, where fixture data was filed alongside live hardcodes because it was
spelled the same way.

The root is NOT under /tmp/ on purpose, and the reason is load-bearing rather
than stylistic: hooks/sandbox_rules.py exempts /tmp/ and /var/tmp/ from its
outside-the-worktree path scan. A fixture rooted there would make the very
assertions these files exist to make — "a write to this path is blocked" — pass
because the path was exempt, not because the rule fired. pytest's tmp_path is
the usual answer for path fixtures and is the wrong answer here for exactly that
reason. backend/tests/test_tool_proxy_sandbox.py carried this warning inline
before this module existed.

Callers that need the state directory interpolate FIXTURE_HOME and then spell
the state-directory name inline, exactly as they did before. It is deliberately
not hoisted into a constant here. That name is the real runtime state directory
on every machine — not a fixture at all — and D#1997 acceptance item 12 pins its
occurrence count across the tracked tree as a tripwire against a careless
regex. Naming it once more in this docstring would have moved that count, which
is how the tripwire earns its keep: it caught this file.
"""

from __future__ import annotations

# Stands in for the directory a checkout sits in — the role a user's home
# directory plays on a real machine, which is why tests/test_classify_cwd.py can
# assert it classifies as untrusted for being the repo's *parent*. Everything
# else here hangs off it, so a future widening of the guard pattern in
# scripts/check-no-hardcoded-checkout-paths.sh is a one-line change rather than
# a sixty-line one.
FIXTURE_HOME = "/synthetic"

# Stands in for the main checkout — what a worktree was branched from, and what
# hooks/repo_root.py's SANDBOX_MAIN_REPO_ROOT override is pinned to by
# tests/conftest.py so that in-process and subprocess callers agree.
FIXTURE_MAIN_REPO = f"{FIXTURE_HOME}/main-repo"

# Claude Code names a project's transcript directory after the checkout path
# with every '/' replaced by '-'. Derived rather than spelled out so the two
# cannot drift apart; no test that asserts *on* the slug transform builds its
# input from this constant.
FIXTURE_PROJECT_SLUG = FIXTURE_MAIN_REPO.replace("/", "-")

__all__ = ["FIXTURE_HOME", "FIXTURE_MAIN_REPO", "FIXTURE_PROJECT_SLUG"]
