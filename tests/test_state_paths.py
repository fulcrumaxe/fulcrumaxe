"""
Tests for backend/state_paths.py

D#1810: STATE_DIR / STATS_DB / STATE_DB / AUDIT_LOG / CIRCUIT_BREAKER_HISTORY
/ BLACKBOARD_DIR / EXTERNAL_INTAKE_BASELINES / PARITY_HISTORY used to be
module-level constants frozen at import time, which defeated a later
AUTONOMOUS_TEAM_STATE_DIR override (the whole test suite was silently
writing into the production runtime-state dir). They are now resolved via a
PEP 562 module __getattr__ on every access, so import order no longer
matters, plus a PYTEST_CURRENT_TEST fail-closed guard that converts a
missed module-level import elsewhere in the tree into a loud failure
instead of a silent write to production.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import backend.state_paths as sp

_REPO_ROOT = Path(__file__).resolve().parent.parent

_STATE_DIR_DERIVED_NAMES = (
    "STATE_DIR",
    "STATS_DB",
    "STATE_DB",
    "AUDIT_LOG",
    "CIRCUIT_BREAKER_HISTORY",
    "BLACKBOARD_DIR",
    "EXTERNAL_INTAKE_BASELINES",
)
_ALL_EXPORTED_NAMES = _STATE_DIR_DERIVED_NAMES + ("PARITY_HISTORY",)


# ---------------------------------------------------------------------------
# Module shape — the PEP 562 mechanism itself
# ---------------------------------------------------------------------------


class TestModuleShape:
    def test_has_module_getattr(self):
        # Spec item 1: `hasattr(s, '__getattr__')` -> True
        assert hasattr(sp, "__getattr__")

    def test_constants_absent_from_module_globals(self):
        # Spec item 2: `'STATS_DB' in vars(s)` -> False. This is what makes
        # __getattr__ fire for these names at all — a name present in
        # module globals never reaches __getattr__.
        for name in _ALL_EXPORTED_NAMES:
            assert name not in vars(sp), f"{name} must not be a module global"


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — pytest guard
# ---------------------------------------------------------------------------


class TestPytestGuard:
    @pytest.mark.parametrize("name", _STATE_DIR_DERIVED_NAMES)
    def test_ac1_guard_raises_without_state_dir_under_pytest(self, monkeypatch, name):
        """AC-1: PYTEST_CURRENT_TEST set, AUTONOMOUS_TEAM_STATE_DIR unset ->
        raise instead of resolving under ~/.autonomous-forever-state/."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "fake::test (call)")
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        monkeypatch.delenv("STATS_DB_PATH", raising=False)

        with pytest.raises(sp.UnsandboxedStatePathError) as excinfo:
            getattr(sp, name)
        # The message must name the offending variable — that's what makes
        # AC-1 actionable for a human, not just AC-5's grep.
        assert name in str(excinfo.value)

    def test_ac2_guard_does_not_misfire_with_state_dir_set(self, monkeypatch, tmp_path):
        """AC-2: PYTEST_CURRENT_TEST set AND AUTONOMOUS_TEAM_STATE_DIR set ->
        every path resolves under the temp dir and nothing raises."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "fake::test (call)")
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("STATS_DB_PATH", raising=False)

        assert sp.STATE_DIR == tmp_path
        assert sp.STATS_DB == tmp_path / "stats.duckdb"
        assert sp.STATE_DB == tmp_path / "state.db"
        assert sp.AUDIT_LOG == tmp_path / "audit.jsonl"
        assert sp.CIRCUIT_BREAKER_HISTORY == tmp_path / "circuit-breaker-history.jsonl"
        assert sp.BLACKBOARD_DIR == tmp_path / "blackboard"
        assert sp.EXTERNAL_INTAKE_BASELINES == tmp_path / "external-intake-baselines.json"

    def test_stats_db_path_override_bypasses_guard(self, monkeypatch, tmp_path):
        """STATS_DB_PATH is an explicit override and wins outright, even
        under pytest with AUTONOMOUS_TEAM_STATE_DIR unset — matches existing
        precedent in agent_run_tracker._db_path(), which checks
        STATS_DB_PATH before anything state-dir-derived."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "fake::test (call)")
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        override = tmp_path / "custom-stats.duckdb"
        monkeypatch.setenv("STATS_DB_PATH", str(override))

        assert sp.STATS_DB == override

    def test_parity_history_not_covered_by_guard(self, monkeypatch, tmp_path):
        """PARITY_HISTORY is repo-relative, not STATE_DIR-derived, and never
        resolves under ~/.autonomous-forever-state/ — the guard's own
        condition (AC-1's wording) doesn't apply to it."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "fake::test (call)")
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        monkeypatch.delenv("PARITY_HISTORY_PATH", raising=False)

        # Must not raise.
        assert sp.PARITY_HISTORY == _REPO_ROOT / ".autonomous-team" / "parity-history.jsonl"

    def test_real_world_verification_probe(self):
        """Mirrors the Discussion's own regression-gate command:
        `PYTEST_CURRENT_TEST=x python3 -c "import backend.state_paths as s; s.STATS_DB"`
        must exit non-zero — this was verified to print the production path
        and exit 0 on main before this Spec."""
        env = dict(os.environ)
        env.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
        env.pop("STATS_DB_PATH", None)
        env["PYTEST_CURRENT_TEST"] = "x"
        result = subprocess.run(
            [sys.executable, "-c", "import backend.state_paths as s; s.STATS_DB"],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"expected non-zero exit; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert ".autonomous-forever-state" not in result.stdout
        assert "NOT ISOLATED" not in result.stdout


# ---------------------------------------------------------------------------
# AC-3 — the actual bug: import-order independence
# ---------------------------------------------------------------------------


class TestCallTimeResolution:
    def test_ac3_agent_run_tracker_db_path_follows_late_env_set(self, monkeypatch, tmp_path):
        """AC-3: backend.state_paths (and, transitively, agent_run_tracker)
        is already imported at collection time in this process — in the
        buggy version that alone was enough to freeze STATS_DB. Set
        AUTONOMOUS_TEAM_STATE_DIR *after* that import and confirm
        agent_run_tracker._db_path() still follows it. This is the exact
        scenario that was broken on main."""
        import backend.agent_run_tracker as art  # already-imported module, on purpose

        monkeypatch.delenv("STATS_DB_PATH", raising=False)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

        result = art._db_path()
        assert str(result).startswith(str(tmp_path)), (
            f"NOT ISOLATED — resolved to {result}, expected under {tmp_path}"
        )


# ---------------------------------------------------------------------------
# AC-4 / AC-5 — no module-level freeze survives, no PR-3 file touched
# ---------------------------------------------------------------------------

# Files that used to carry D#1908 PR 3 work — the legacy `legacy.exists() and
# not legacy.is_symlink()` branch that points a resolver at an in-repo copy.
#
# This set used to hold eleven names. Nine of them came off it in D#1967, which
# had to delete those files' duplicate stats.duckdb / audit.jsonl resolvers
# outright to get to one resolver per path; removing the resolver removed the
# legacy branch with it, so there is no PR 3 work left in them to reserve:
#   agent_run_reader, agent_run_tracker, migrations/001_drop_dead_metrics,
#   stats/anomaly_detector, stats/scheduled_jobs, stats/sdk_vs_cc,
#   stats_freshness_watchdog, stats_reader, stats_writer.
# The last two — backend/blackboard.py and backend/migrate_to_sqlite.py (the
# latter reserved by name only; it is an unrelated flat-file migration, not
# touched by this work) — came off in D#1908 PR 3 itself, which deleted the
# is_symlink() branch from backend/blackboard.py and backend/db.py (the only
# two `grep -rn "is_symlink()" --include=*.py backend/` matches at spec time)
# and collapsed both onto state_paths.resolve(). The reservation is
# discharged; the set stays empty rather than being deleted so a future PR
# that reintroduces this pattern somewhere has an obvious place to add itself
# back.
_PR3_FORBIDDEN_FILES: set[str] = set()

_CONST_IMPORT_RE = re.compile(
    r"^from\s+(backend\.)?state_paths\s+import\s+"
    r"(STATE_DIR|STATS_DB|STATE_DB|AUDIT_LOG|CIRCUIT_BREAKER_HISTORY|"
    r"BLACKBOARD_DIR|EXTERNAL_INTAKE_BASELINES|PARITY_HISTORY)\b"
)

_DB_PY_FREEZE_RE = re.compile(r"^_?[A-Z_]+\s*=\s*_?[a-z_]+\(\)\s*$")


def _iter_source_files():
    for pattern in ("*.py", "*.sh"):
        for path in _REPO_ROOT.rglob(pattern):
            rel = path.relative_to(_REPO_ROOT)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] in ("archive", "tests", ".git"):
                continue
            if "tests" in parts:
                continue
            if ".claude" in parts:
                continue
            yield path, rel


class TestNoModuleLevelFreeze:
    def test_ac5_no_module_level_state_paths_constant_import(self):
        """AC-5 (import half): grep the tree (excluding archive/ and tests)
        for `^from (backend\\.)?state_paths import <CONST>` — zero matches.
        This is the enumeration guarantee: it is why AC-1 ships in the same
        PR rather than trusting a hand-built file list."""
        offenders = []
        for path, rel in _iter_source_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _CONST_IMPORT_RE.match(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert offenders == [], "module-level state_paths constant import(s):\n" + "\n".join(offenders)

    def test_ac5_no_module_level_freeze_in_db_py(self):
        """AC-5 (db.py half): no `_?[A-Z_]+ *= *_?[a-z_]+\\(\\)` pattern
        survives in backend/db.py. This is the one sanctioned D#1810
        crossing into db.py (line 46 on main: `_DB_PATH =
        _resolve_db_path()`) — it is the only instance of this pattern in
        the tree, and it must not come back."""
        db_py = _REPO_ROOT / "backend" / "db.py"
        offenders = [
            f"backend/db.py:{i}: {line.strip()}"
            for i, line in enumerate(db_py.read_text(encoding="utf-8").splitlines(), start=1)
            if _DB_PY_FREEZE_RE.match(line)
        ]
        assert offenders == [], offenders

    def test_ac4_no_pr3_forbidden_file_in_diff(self):
        """AC-4: git diff --name-only origin/main...HEAD must not contain a
        file that still has D#1908 PR 3 work in it. Skips gracefully if
        origin/main isn't available (e.g. a shallow or offline checkout).

        _PR3_FORBIDDEN_FILES is empty as of D#1908 PR 3 — see the comment on
        it for the history. This assertion is a no-op set intersection now,
        kept so a future PR that reintroduces the legacy-branch pattern has
        an existing hook to reserve a file against. The db.py lines-38-41
        range guard that used to live here was removed in the same PR: PR 3
        deleted those exact lines (the legacy is_symlink() branch) on
        purpose, so a guard forbidding that edit would fail PR 3's own
        branch."""
        result = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"origin/main not available for diff: {result.stderr.strip()}")
        changed = set(result.stdout.splitlines())
        touched_pr3 = changed & _PR3_FORBIDDEN_FILES
        assert not touched_pr3, f"touched D#1908 PR 3 files: {touched_pr3}"


# ---------------------------------------------------------------------------
# AC-7 — resolved values unchanged (behaviour-preserving)
# ---------------------------------------------------------------------------


class TestBehaviorPreserving:
    @pytest.mark.parametrize(
        "name,suffix",
        [
            ("STATS_DB", "stats.duckdb"),
            ("STATE_DB", "state.db"),
            ("AUDIT_LOG", "audit.jsonl"),
            ("CIRCUIT_BREAKER_HISTORY", "circuit-breaker-history.jsonl"),
            ("BLACKBOARD_DIR", "blackboard"),
            ("EXTERNAL_INTAKE_BASELINES", "external-intake-baselines.json"),
        ],
    )
    def test_ac7_state_dir_derived_paths_unchanged(self, monkeypatch, tmp_path, name, suffix):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("STATS_DB_PATH", raising=False)
        assert getattr(sp, name) == tmp_path / suffix

    def test_ac7_state_dir_itself_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        assert sp.STATE_DIR == tmp_path

    def test_ac7_default_state_dir_unchanged(self, monkeypatch):
        """No override, and not under pytest (guard cleared) — matches the
        pre-D#1810 default exactly: ~/.autonomous-forever-state/."""
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        monkeypatch.delenv("STATS_DB_PATH", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert sp.STATE_DIR == Path.home() / ".autonomous-forever-state"
        assert sp.STATS_DB == Path.home() / ".autonomous-forever-state" / "stats.duckdb"

    def test_ac7_parity_history_unchanged(self, monkeypatch):
        monkeypatch.delenv("PARITY_HISTORY_PATH", raising=False)
        assert sp.PARITY_HISTORY == _REPO_ROOT / ".autonomous-team" / "parity-history.jsonl"

    def test_ac7_parity_history_path_override_unchanged(self, monkeypatch, tmp_path):
        override = tmp_path / "custom-parity-history.jsonl"
        monkeypatch.setenv("PARITY_HISTORY_PATH", str(override))
        assert sp.PARITY_HISTORY == override


# ---------------------------------------------------------------------------
# AC-8 — STATS_DB_PATH folded into the single resolver
# ---------------------------------------------------------------------------


class TestStatsDbPathFolded:
    def test_ac8_documented_in_module_docstring(self):
        assert "STATS_DB_PATH" in (sp.__doc__ or "")

    def test_ac8_stats_db_path_takes_precedence_over_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))
        override = tmp_path / "override.duckdb"
        monkeypatch.setenv("STATS_DB_PATH", str(override))
        assert sp.STATS_DB == override

    def test_ac8_falls_back_to_state_dir_when_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("STATS_DB_PATH", raising=False)
        assert sp.STATS_DB == tmp_path / "stats.duckdb"


# ---------------------------------------------------------------------------
# AC-9 — for_project() and ensure_state_dir() untouched in behaviour
# ---------------------------------------------------------------------------


class TestEnsureStateDir:
    def test_creates_missing_dir(self, monkeypatch, tmp_path):
        target = tmp_path / "brand-new-state"
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(target))
        assert not target.exists()

        sp.ensure_state_dir()

        assert target.is_dir()

    def test_creates_blackboard_subdir(self, monkeypatch, tmp_path):
        target = tmp_path / "state-with-bb"
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(target))

        sp.ensure_state_dir()

        assert (target / "blackboard").is_dir()

    def test_idempotent_when_dir_exists(self, monkeypatch, tmp_path):
        target = tmp_path / "existing-state"
        target.mkdir()
        (target / "blackboard").mkdir()
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(target))

        # Should not raise
        sp.ensure_state_dir()
        sp.ensure_state_dir()

        assert target.is_dir()

    def test_returns_state_dir(self, monkeypatch, tmp_path):
        target = tmp_path / "ret-test"
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(target))
        result = sp.ensure_state_dir()
        assert result == target


class TestForProject:
    def test_for_project_fallback_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        paths = sp.for_project("nonexistent-project")
        assert paths.state_dir == tmp_path / ".nonexistent-project-state"
        assert paths.repo is None


class TestForProjectServedStateDir:
    """for_project() consults this process's own STATE_DIR (D#2259) before
    falling back to the home-anchored conventions, so an adopter whose state
    dir sits outside $HOME can resolve without a marker file under $HOME."""

    def test_served_dir_match_by_project_name(self, monkeypatch, tmp_path):
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        served_dir.mkdir(parents=True)
        (served_dir / "dashboard-runtime.json").write_text(
            json.dumps(
                {
                    "project_name": "gatekeep",
                    "project_repo": "autonomous-agent-7/fulcrumaxe",
                    "state_dir": str(served_dir),
                }
            )
        )
        monkeypatch.setattr(Path, "home", lambda: empty_home)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

        paths = sp.for_project("gatekeep")

        assert paths.state_dir == served_dir
        assert paths.stats_db == served_dir / "stats.duckdb"
        assert paths.repo == "autonomous-agent-7/fulcrumaxe"

    def test_served_dir_name_mismatch_declines(self, monkeypatch, tmp_path):
        """A served dir belonging to a different project must decline, not
        hijack — the request falls through to the home-anchored convention."""
        home = tmp_path / "home"
        home.mkdir()
        served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        served_dir.mkdir(parents=True)
        (served_dir / "dashboard-runtime.json").write_text(
            json.dumps({"project_name": "gatekeep", "state_dir": str(served_dir)})
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

        paths = sp.for_project("autonomous-forever")

        assert paths.state_dir == home / ".autonomous-forever-state"

    def test_served_dir_unresolvable_under_pytest_falls_back(self, monkeypatch, tmp_path):
        """AUTONOMOUS_TEAM_STATE_DIR unset under pytest -> _state_dir() raises
        UnsandboxedStatePathError; _served_state_dir() must swallow it and
        for_project() must still return the home-anchored fallback."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)

        paths = sp.for_project("nonexistent-project")

        assert paths.state_dir == tmp_path / ".nonexistent-project-state"
        assert paths.repo is None

    def test_served_dir_relative_falls_back(self, monkeypatch, tmp_path):
        """A relative AUTONOMOUS_TEAM_STATE_DIR raises RelativeStateDirError
        from _state_dir() — that must be swallowed too."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", "relative-junk")

        paths = sp.for_project("anything")

        assert paths.state_dir == tmp_path / ".anything-state"

    def test_served_dir_malformed_json_falls_back(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        served_dir.mkdir(parents=True)
        (served_dir / "dashboard-runtime.json").write_text("not json{")
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

        paths = sp.for_project("gatekeep")

        assert paths.state_dir == home / ".gatekeep-state"

    def test_served_dir_missing_runtime_file_falls_back(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        served_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

        paths = sp.for_project("gatekeep")

        assert paths.state_dir == home / ".gatekeep-state"

    def test_served_dir_legacy_file_matches_by_directory_basename(self, monkeypatch, tmp_path):
        """A runtime file predating the project_name field is still matched,
        via the directory basename convention."""
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        served_dir = tmp_path / "elsewhere" / ".gatekeep-state"
        served_dir.mkdir(parents=True)
        (served_dir / "dashboard-runtime.json").write_text(json.dumps({"ports": {"vite": 5188}}))
        monkeypatch.setattr(Path, "home", lambda: empty_home)
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(served_dir))

        paths = sp.for_project("gatekeep")

        assert paths.state_dir == served_dir
