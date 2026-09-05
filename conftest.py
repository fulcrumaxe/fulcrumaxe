# conftest.py (repo root) — keep archive/ out of diff-aware test selection.
#
# scripts/preflight-fast.sh runs pytest-picked (--picked --mode=branch) to
# select only tests touched by the current diff. pytest-picked's selection
# is a plain `git diff --name-status` against main, so any file moved via
# `git mv` — including into archive/ under the Archive Protocol (see
# CLAUDE.md) — shows up as "changed" and gets passed to pytest as an
# explicit collection target. pytest has no config-level way to override an
# explicitly-named collection target (`--ignore`, `--ignore-glob`, and a
# conftest's `collect_ignore*` all apply only to directory-walk discovery,
# not explicit args), so a large archival PR — this repo's own D#1890 being
# the first to hit it, moving 400+ files including several test files with
# now-broken relative sibling imports scoped to their old location — makes
# preflight-fast.sh red for a reason that has nothing to do with whether the
# diff is correct.
#
# Filtering config.args post-hoc, after pytest-picked's own pytest_configure
# has populated it (hence trylast=True — plugins are expected to run before
# conftest.py hooks of the same name, but trylast makes the ordering
# explicit rather than relying on default registration order), is the one
# hook that actually works for an explicitly-named path. Only touches
# archive/-prefixed entries; every other picked/testmon/normal pytest
# invocation is unaffected.
import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    args = getattr(config, "args", None)
    if args:
        config.args = [a for a in args if not str(a).startswith("archive/")]
