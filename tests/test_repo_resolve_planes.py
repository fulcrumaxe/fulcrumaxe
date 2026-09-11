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
        {"repo": "owner/r", "code_repo": "   "},
        {"repo": "owner/r", "code_repo": 42},
        {"repo": "owner/r", "code_repo": {"nested": "no"}},
    ],
    ids=["empty-string", "whitespace-only", "non-string", "object"],
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


# --- D#2536: a whitespace-only value defeats a bare `[[ -n ]]` gate ---------
#
# `_resolve_code_repo`/`_resolve_discussion_repo` already normalize
# whitespace-only input above (it falls through like an empty value). This
# section covers the two properties D#2536 is specifically about: the
# `_require_code_repo` chokepoint's own independent guard, and keeping
# "absent" and "whitespace-only" distinguishable for the Discussion plane.


def test_require_code_repo_rejects_whitespace_only(tmp_path):
    """`_require_code_repo` prints nothing on stdout, an actionable message
    on stderr, and returns 1 for a whitespace-only resolution — the same
    contract it already has for an empty one, not the pre-fix `[[ -z "$r" ]]`
    gate that a whitespace-only value silently passes through."""
    fake = _fake_repo(tmp_path, None)
    runner = fake / "runner.sh"
    runner.write_text(
        'source "$(dirname "$0")/scripts/lib/repo-resolve.sh"\n'
        "AUTONOMOUS_TEAM_REPO='   '\n"
        '_require_code_repo "test-context"\n'
    )
    env = os.environ.copy()
    env.pop("AUTONOMOUS_TEAM_REPO", None)
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "could not resolve the code repo" in proc.stderr


def test_require_code_repo_still_accepts_well_formed(tmp_path):
    """Sanity check alongside the rejection test above: a real slug is
    unaffected by the whitespace fix."""
    out, rc = _call(tmp_path, "_require_code_repo", {"code_repo": "owner/public"})
    assert (out, rc) == ("owner/public", 0)


def test_absent_and_whitespace_only_discussion_repo_both_fall_through_the_same_way(
    tmp_path,
):
    """THE TRAP (D#2536 item 6): an absent discussion_repo is legitimately
    empty-and-fine — not an error — for a fork with no private twin. A
    blanket "empty is always an error" fix would break every such fork. This
    asserts the two stay distinguishable in cause (one key is missing, the
    other is present-but-degenerate) while agreeing in effect: both fall
    through the precedence chain exactly like any other absent value,
    landing on whatever "repo" resolves to, or on empty-and-fine when
    nothing resolves at all — neither ever surfaces as an error, and neither
    is ever returned as a literal "   "."""
    config_with_repo = {"repo": "owner/legacy"}
    absent, absent_rc = _call(
        tmp_path / "absent", "_resolve_discussion_repo", config_with_repo
    )
    whitespace, whitespace_rc = _call(
        tmp_path / "whitespace",
        "_resolve_discussion_repo",
        {**config_with_repo, "discussion_repo": "   "},
    )
    assert (absent, absent_rc) == ("owner/legacy", 0)
    assert (whitespace, whitespace_rc) == ("owner/legacy", 0)

    # And with nothing at all configured, both land on empty-and-fine.
    absent_nothing, absent_nothing_rc = _call(
        tmp_path / "absent-nothing", "_resolve_discussion_repo", {}
    )
    whitespace_nothing, whitespace_nothing_rc = _call(
        tmp_path / "whitespace-nothing",
        "_resolve_discussion_repo",
        {"discussion_repo": "   "},
    )
    assert (absent_nothing, absent_nothing_rc) == ("", 0)
    assert (whitespace_nothing, whitespace_nothing_rc) == ("", 0)
