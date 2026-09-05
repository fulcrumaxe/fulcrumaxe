"""tests/test_repo_resolve_planes.py

Tests for the code-plane / Discussion-plane accessors in
scripts/lib/repo-resolve.sh: _resolve_code_repo and _resolve_discussion_repo.

The point of these two functions is that they are inert. With neither
"code_repo" nor "discussion_repo" set in .autonomous-team/config.json — which
is the state of every tree today — both must return byte-for-byte what
_resolve_repo returns, so introducing the vocabulary retargets nothing.

The one behaviour that is not inert is the empty case: an unresolvable
Discussion plane is a legitimate state for a fork with no private twin, so
_resolve_discussion_repo returns exit 0 and no output rather than failing.
_resolve_code_repo keeps _resolve_repo's fail-loudly behaviour, because a
checkout with no code repo really is broken.

Every case runs against a throwaway fake repo under tmp_path. Nothing here
reads or writes the live .autonomous-team tree.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_RESOLVE_SH = REPO_ROOT / "scripts" / "lib" / "repo-resolve.sh"


def _fake_repo(tmp_path: Path, config: dict | None) -> Path:
    """Build a minimal tree with repo-resolve.sh at its real relative path."""
    fake = tmp_path / "fake-repo"
    (fake / "scripts" / "lib").mkdir(parents=True)
    (fake / ".autonomous-team").mkdir()
    shutil.copy(REPO_RESOLVE_SH, fake / "scripts" / "lib" / "repo-resolve.sh")
    if config is not None:
        (fake / ".autonomous-team" / "config.json").write_text(json.dumps(config))
    return fake


def _call(tmp_path: Path, func: str, config: dict | None, env_repo: str | None = None):
    """Source repo-resolve.sh in a fake repo, call *func*, return (stdout, rc)."""
    fake = _fake_repo(tmp_path, config)
    runner = fake / "runner.sh"
    runner.write_text(
        'source "$(dirname "$0")/scripts/lib/repo-resolve.sh"\n' f"{func}\n"
    )

    env = os.environ.copy()
    env.pop("AUTONOMOUS_TEAM_REPO", None)
    if env_repo is not None:
        env["AUTONOMOUS_TEAM_REPO"] = env_repo

    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, env=env
    )
    return proc.stdout.strip(), proc.returncode


# --- The inert case: both keys absent ---------------------------------------


@pytest.mark.parametrize(
    "config,env_repo",
    [
        ({"repo": "owner/from-config"}, None),
        ({"language": "en"}, "owner/from-env"),
        (None, "owner/from-env"),
    ],
    ids=["config-json", "env-var-with-config-present", "env-var-no-config"],
)
def test_both_accessors_match_resolve_repo_when_keys_absent(
    tmp_path, config, env_repo
):
    """With neither new key set, all three functions agree exactly.

    This is the property the whole change rests on: adding the accessors moves
    no call site because there is nothing to move to.
    """
    baseline, baseline_rc = _call(tmp_path / "a", "_resolve_repo", config, env_repo)
    code, code_rc = _call(tmp_path / "b", "_resolve_code_repo", config, env_repo)
    disc, disc_rc = _call(tmp_path / "c", "_resolve_discussion_repo", config, env_repo)

    assert baseline_rc == 0
    assert code == baseline
    assert disc == baseline
    assert code_rc == baseline_rc == disc_rc == 0


# --- The configured case: keys take effect ----------------------------------


def test_code_repo_key_wins_over_repo(tmp_path):
    config = {"repo": "owner/private", "code_repo": "owner/public"}
    out, rc = _call(tmp_path, "_resolve_code_repo", config)
    assert (out, rc) == ("owner/public", 0)


def test_discussion_repo_key_wins_over_repo(tmp_path):
    config = {"repo": "owner/public", "discussion_repo": "owner/private"}
    out, rc = _call(tmp_path, "_resolve_discussion_repo", config)
    assert (out, rc) == ("owner/private", 0)


def test_config_keys_outrank_the_env_var_here(tmp_path):
    """Deliberately the opposite of backend/_repo.py's accessors.

    repo-resolve.sh documents config.json ahead of AUTONOMOUS_TEAM_REPO, and
    ts-backend/src/config/repo.ts freezes the same order under D#1632, while
    backend/_repo.py documents the environment as highest priority. Each
    accessor obeys the resolver it lives in rather than being unified, because
    unifying them means breaking one of the two documented contracts. This test
    exists so that asymmetry reads as a decision, not an oversight — if you are
    here because it looks wrong, read _repo_planes.py's module docstring first.
    """
    config = {"repo": "owner/config", "code_repo": "owner/public"}
    out, rc = _call(tmp_path, "_resolve_code_repo", config, env_repo="owner/env")
    assert (out, rc) == ("owner/public", 0)


def test_split_planes_resolve_independently(tmp_path):
    """The whole point: one config, two different answers."""
    config = {
        "repo": "owner/legacy",
        "code_repo": "owner/public",
        "discussion_repo": "owner/private",
    }
    code, _ = _call(tmp_path / "a", "_resolve_code_repo", config)
    disc, _ = _call(tmp_path / "b", "_resolve_discussion_repo", config)
    assert code == "owner/public"
    assert disc == "owner/private"
    assert code != disc


# --- The asymmetry: empty is not an error for the Discussion plane ----------


def test_discussion_repo_empty_is_not_an_error(tmp_path):
    """A fork has no private twin. That is a state, not a failure.

    Nothing configured at all: _resolve_discussion_repo must exit 0 with no
    stdout so callers can branch on the empty string.
    """
    out, rc = _call(tmp_path, "_resolve_discussion_repo", None)
    assert rc == 0, "empty Discussion plane must not be reported as a failure"
    assert out == ""


def test_discussion_repo_does_not_inherit_a_hardcoded_slug(tmp_path):
    """The D#1870 hazard, asserted directly rather than by inspection."""
    out, _ = _call(tmp_path, "_resolve_discussion_repo", None)
    assert "fulcrumaxe" not in out
    assert "autonomous-agent-7" not in out


def test_code_repo_still_fails_loudly_when_nothing_resolves(tmp_path):
    """_resolve_code_repo keeps _resolve_repo's fail-loudly contract."""
    out, rc = _call(tmp_path, "_resolve_code_repo", None)
    assert rc == 1
    assert out == ""


# --- Malformed input is "not configured", not a crash -----------------------


@pytest.mark.parametrize(
    "config",
    [
        {"repo": "owner/r", "code_repo": ""},
        {"repo": "owner/r", "code_repo": 42},
        {"repo": "owner/r", "code_repo": {"nested": "no"}},
    ],
    ids=["empty-string", "non-string", "object"],
)
def test_unusable_code_repo_value_falls_back_to_repo(tmp_path, config):
    out, rc = _call(tmp_path, "_resolve_code_repo", config)
    assert (out, rc) == ("owner/r", 0)


def test_malformed_config_json_falls_back_rather_than_crashing(tmp_path):
    fake = _fake_repo(tmp_path, None)
    (fake / ".autonomous-team" / "config.json").write_text("{not json")
    runner = fake / "runner.sh"
    runner.write_text(
        'source "$(dirname "$0")/scripts/lib/repo-resolve.sh"\n'
        "_resolve_discussion_repo\n"
    )
    env = os.environ.copy()
    env["AUTONOMOUS_TEAM_REPO"] = "owner/from-env"
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "owner/from-env"
