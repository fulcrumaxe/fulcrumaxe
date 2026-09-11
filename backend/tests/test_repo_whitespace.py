"""backend/tests/test_repo_whitespace.py

D#2536: a whitespace-only repo slug must be treated as absent at every
precedence step in backend/_repo.py's _load_repo(), the same way an empty
string already is.

Before this fix, every gate in _load_repo() was a bare truthiness check
(`if value:`), which a whitespace-only string passes. Watched failing first
(D#2536 item 1): with AUTONOMOUS_TEAM_REPO="   " and nothing else configured,
`python3 -c "import backend._repo"` raised
`ValueError: not enough values to unpack (expected 2, got 1)` at module
import — from `"   ".split("/", 1)` at backend/_repo.py's REPO_OWNER/REPO_NAME
line. That exception names nothing about repo resolution. The fix is
`_non_empty()`, applied at _read_project_json (covering the state-dir and
repo-root project.json steps) and at each remaining gate, so a degenerate
value can never survive to the unpack — the terminal case is this module's
own actionable RuntimeError instead.

Follows the isolation pattern tests/test_backend_repo_py.py already
established for this module: reload backend._repo with controlled env vars,
and monkeypatch _read_project_json directly to bypass the repo-root
.autonomous-team/project.json and .git origin-remote steps, which are not
overridable via env and depend on the checkout backend/_repo.py happens to
live in.

Run:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" \
        python -m pytest backend/tests/test_repo_whitespace.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest


def _reload_repo_module(monkeypatch, env: dict[str, str | None]) -> object:
    """Reload backend._repo with the given env vars applied."""
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)

    for mod_name in list(sys.modules):
        if "_repo" in mod_name and "backend" in mod_name:
            del sys.modules[mod_name]

    import backend._repo as repo_mod

    importlib.reload(repo_mod)
    return repo_mod


def _reload_with_no_fallback(monkeypatch, env: dict[str, str | None]) -> object:
    """Reload backend._repo, then bypass the repo-root project.json and .git
    origin-remote steps, so only the env var under test decides the outcome
    of a later, explicit _load_repo() call.

    Both bypassed steps are environment-dependent (real in a full checkout of
    this repo, absent in a bare export) — module-level import itself must
    succeed either way, so the initial reload first points at a throwaway
    state-dir project.json that always resolves, and the monkeypatches are
    applied *after* that reload succeeds, before *env* (the input actually
    under test) is applied and before the caller's own _load_repo() call.
    """
    safe_dir = tempfile.mkdtemp()
    (Path(safe_dir) / "project.json").write_text(
        json.dumps({"repo": "placeholder/placeholder"})
    )
    mod = _reload_repo_module(
        monkeypatch,
        {"AUTONOMOUS_TEAM_REPO": None, "AUTONOMOUS_TEAM_STATE_DIR": safe_dir},
    )
    monkeypatch.setattr(mod, "_read_project_json", lambda path: None)
    monkeypatch.setattr(mod, "repo_slug_from_git_config", lambda repo_root: None)
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    return mod


class TestNonEmptyHelper:
    """Direct unit coverage of the new normalizer."""

    def test_well_formed_value_passes_through_unchanged(self) -> None:
        import backend._repo as repo_mod

        assert repo_mod._non_empty("acme/widgets") == "acme/widgets"

    @pytest.mark.parametrize(
        "value", ["", "   ", "\t\n ", None, 42, {"a": 1}, []], ids=lambda v: repr(v)
    )
    def test_degenerate_values_are_absent(self, value) -> None:
        import backend._repo as repo_mod

        assert repo_mod._non_empty(value) is None

    def test_surrounding_whitespace_on_a_real_value_is_not_stripped(self) -> None:
        """A presence test, not a normalizer — mirrors ts-backend's
        nonEmpty() (D#2520)."""
        import backend._repo as repo_mod

        assert repo_mod._non_empty("  acme/widgets  ") == "  acme/widgets  "


class TestReadProjectJsonTreatsWhitespaceAsAbsent:
    def test_whitespace_only_repo_field(self, tmp_path: Path) -> None:
        pj = tmp_path / "project.json"
        pj.write_text(json.dumps({"repo": "   "}))

        import backend._repo as repo_mod

        assert repo_mod._read_project_json(pj) is None

    def test_well_formed_repo_field_still_resolves(self, tmp_path: Path) -> None:
        pj = tmp_path / "project.json"
        pj.write_text(json.dumps({"repo": "acme/widgets"}))

        import backend._repo as repo_mod

        assert repo_mod._read_project_json(pj) == "acme/widgets"


class TestLoadRepoWhitespaceHandling:
    """D#2536 item 4: whitespace-only is treated as absent at every step of
    _load_repo(), so it falls through to the next source and ultimately
    raises this module's own RuntimeError — never a ValueError."""

    def test_whitespace_only_env_var_falls_through_to_state_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(
            json.dumps({"repo": "state-dir/repo"})
        )

        mod = _reload_repo_module(
            monkeypatch,
            {
                "AUTONOMOUS_TEAM_REPO": "   ",
                "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            },
        )
        assert mod.REPO == "state-dir/repo"

    def test_whitespace_only_state_dir_repo_falls_through_to_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "project.json").write_text(json.dumps({"repo": "   "}))

        mod = _reload_repo_module(
            monkeypatch,
            {
                "AUTONOMOUS_TEAM_REPO": "org/from-env",
                "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir),
            },
        )
        assert mod.REPO == "org/from-env"

    def test_raises_runtime_error_not_value_error_on_whitespace_only(
        self, monkeypatch
    ) -> None:
        """THE regression D#2536 is about. Before the fix, this raised
        `ValueError: not enough values to unpack (expected 2, got 1)` at
        the REPO_OWNER, REPO_NAME = REPO.split("/", 1) line — an exception
        that names nothing about repo resolution."""
        mod = _reload_with_no_fallback(
            monkeypatch,
            {
                "AUTONOMOUS_TEAM_REPO": "   ",
                "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-no-json-here",
            },
        )

        with pytest.raises(RuntimeError, match="AUTONOMOUS_TEAM_REPO") as exc_info:
            mod._load_repo()
        assert not isinstance(exc_info.value, ValueError)
        assert "project.json" in str(exc_info.value)

    def test_no_resolver_output_is_ever_a_degenerate_value(self, monkeypatch) -> None:
        """No input in this class produces a non-empty REPO that isn't a
        well-formed OWNER/NAME — either a real slug or a loud failure."""
        for env in (
            {
                "AUTONOMOUS_TEAM_REPO": "   ",
                "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-no-json-here",
            },
            {
                "AUTONOMOUS_TEAM_REPO": None,
                "AUTONOMOUS_TEAM_STATE_DIR": "/nonexistent-no-json-here",
            },
        ):
            mod = _reload_with_no_fallback(monkeypatch, env)
            try:
                result = mod._load_repo()
            except RuntimeError:
                continue
            assert "/" in result and result.strip() == result, (
                f"got a degenerate result: {result!r}"
            )
