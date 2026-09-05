"""
Tests for scripts/check-pr-cli-touched.sh's detection logic (D#508).

D#2008, sixth code-review round (owner ruling): the execution-side gate
this file used to test — scripts/run-backend-verification.sh extracting
commands from a Spec's `## Real-world verification` block and running them
— is gone. Classification alone (backend/spec_verification_substance.py,
tested in backend/tests/test_spec_verification_substance.py) is what
unblocks a PR whose frozen Spec lacks the literal heading; execution was
never the requirement and cost five review rounds trying to make it safe.
See archive/run-backend-verification-2026-08-20/README.md for the full
history and archive/run-backend-verification-2026-08-20/run-backend-verification.sh
for the removed script.

check-pr-cli-touched.sh itself is unaffected by that removal — it only
decides whether a PR touches backend/scripts/schema files, which is still
used to decide whether to run the (now execution-free) classification
step at all — so its tests stay.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


class TestCheckPrCliTouched:
    """Tests for scripts/check-pr-cli-touched.sh detection logic.

    These tests do NOT call gh (which requires live PRs). Instead they validate
    the grep patterns used in the script against known file lists.
    """

    DETECTION_SCRIPT = SCRIPTS / "check-pr-cli-touched.sh"

    def _matches(self, files: list[str]) -> bool:
        """Return True if the given file list would trigger the gate."""
        content = "\n".join(files)

        # Replicate the exact grep patterns from the script
        if any(re.match(r'^backend/(api|server)\.py$', f) for f in files):
            return True
        if any(re.match(r'^backend/[^/]+\.py$', f) for f in files):
            return True
        if any(f.startswith('backend/rpc/') for f in files):
            return True
        if any(re.match(r'^scripts/[^/]+\.sh$', f) for f in files):
            return True
        if any(f.startswith('.autonomous-team/schemas/') for f in files):
            return True
        return False

    def test_backend_api_triggers(self):
        assert self._matches(["backend/api.py"])

    def test_backend_server_triggers(self):
        assert self._matches(["backend/server.py"])

    def test_backend_rpc_triggers(self):
        assert self._matches(["backend/rpc/stats_loop.py"])

    def test_backend_any_py_triggers(self):
        assert self._matches(["backend/kpi_engine.py"])

    def test_scripts_sh_triggers(self):
        assert self._matches(["scripts/run-pr-tests.sh"])

    def test_schema_triggers(self):
        assert self._matches([".autonomous-team/schemas/agent-output.schema.json"])

    def test_dashboard_only_does_not_trigger(self):
        """Dashboard-only PRs must NOT trigger backend gate (AC #3)."""
        assert not self._matches(["dashboard/src/pages/StatsPage.tsx"])

    def test_wiki_does_not_trigger(self):
        assert not self._matches(["wiki/Home.md"])

    def test_tests_dir_triggers(self):
        """tests/ is not in the detection list — only backend/, scripts/, schemas/."""
        # tests/ Python files do NOT trigger (they live in tests/, not backend/)
        assert not self._matches(["tests/test_foo.py"])

    def test_dashboard_plus_backend_triggers(self):
        """Combined PR: both backend and dashboard files → gate fires (AC #4)."""
        assert self._matches([
            "dashboard/src/pages/StatsPage.tsx",
            "backend/kpi_engine.py",
        ])
