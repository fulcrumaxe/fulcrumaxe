"""The fixture-rendering mechanism has to be testable, or it is decoration.

D#1997 replaced the recorded machine paths in the transcript fixtures with
placeholder tokens that testsupport/transcript_fixtures.py substitutes at load
time. Everything about that change is invisible to the classifier suites: they
key on path *shape*, so they passed with the old stale root, they pass with the
new rendered one, and they would pass just as happily if the substitution
silently stopped happening and every fixture kept a literal "{{REPO_ROOT}}" in
its paths.

That is the shape D#1984 collects — a change whose correctness no test can
observe. These tests are the observer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.repo_root import main_repo_root
from testsupport.transcript_fixtures import render_fixture, render_text, substitutions

FIXTURES_ROOT = Path(__file__).parent / "fixtures"

# Same pattern scripts/check-no-hardcoded-checkout-paths.sh scans with. Kept in
# sync by hand deliberately: this asserts the *fixture tree* specifically, which
# the guard covers only as long as nobody adds an allowlist entry for it. The
# whole point of D#1997 acceptance item 15 is that this subtree stays at zero
# whether or not the allowlist would have tolerated an entry.
_HOME_CHECKOUT = re.compile(r"/home/" + r"(agent|jp)")

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")


def _fixture_id(path: Path) -> str:
    """Fixture basenames repeat across classifier directories; the relative
    path is what makes a failure report point at one file."""
    return str(path.relative_to(FIXTURES_ROOT))


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES_ROOT.rglob("*") if p.is_file())


def test_fixture_tree_is_non_empty():
    """Guard against the rest of this file passing because it found nothing."""
    assert len(_fixture_files()) > 5


@pytest.mark.parametrize("path", _fixture_files(), ids=_fixture_id)
def test_no_fixture_names_a_real_home_checkout(path: Path):
    """AC#15: no fixture pins a path under somebody's home directory.

    Failing this means a fixture was re-pinned to whichever machine last
    touched it, which is the defect the placeholders replaced rather than a new
    one.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    hit = _HOME_CHECKOUT.search(text)
    assert hit is None, (
        f"{path.relative_to(FIXTURES_ROOT)} names a home-directory checkout "
        f"at offset {hit.start() if hit else -1}; use a placeholder token and "
        f"let testsupport/transcript_fixtures.py render it"
    )


def _templated_fixtures() -> list[Path]:
    return [p for p in _fixture_files()
            if _PLACEHOLDER.search(p.read_text(encoding="utf-8", errors="replace"))]


def test_some_fixtures_are_templated():
    """If this goes to zero the parametrisation was reverted, not completed."""
    assert _templated_fixtures(), "no fixture carries a placeholder token any more"


@pytest.mark.parametrize("path", _templated_fixtures(), ids=_fixture_id)
def test_placeholders_are_fully_substituted(path: Path):
    rendered = render_fixture(path).read_text(encoding="utf-8")
    leftover = _PLACEHOLDER.search(rendered)
    assert leftover is None, (
        f"{path.name} still contains {leftover.group() if leftover else ''} "
        f"after rendering — the token has no entry in the substitution table"
    )


@pytest.mark.parametrize("path", _templated_fixtures(), ids=_fixture_id)
def test_every_token_resolves_to_this_machines_value(path: Path):
    """Each token a fixture uses must be replaced by this machine's value.

    Asserted per token rather than "the repo root appears somewhere", because
    the fixtures do not all use the same ones: the transcript-directory fixtures
    carry {{HOME}} and {{PROJECT_SLUG}} and never mention the root in slash
    form. A blanket check passes for the wrong reason on one group and fails for
    the wrong reason on the other.
    """
    source = path.read_text(encoding="utf-8")
    rendered = render_fixture(path).read_text(encoding="utf-8")
    used = [tok for tok in substitutions() if tok in source]
    assert used, "fixture matched the placeholder pattern but uses no known token"
    for token in used:
        assert substitutions()[token] in rendered, (
            f"{token} did not resolve to {substitutions()[token]!r}"
        )


@pytest.mark.parametrize("path", _templated_fixtures(), ids=_fixture_id)
def test_rendered_copy_keeps_the_source_basename(path: Path):
    """run_analyst derives an agent id from the transcript filename.

    transcript_reader.agent_id_from_path takes path.stem, so a scratch copy
    under a different name would silently change the id every assertion in the
    classifier suites is written against.
    """
    assert render_fixture(path).name == path.name


def test_untemplated_fixture_is_returned_untouched():
    """No scratch copy for a fixture that needs no substitution."""
    plain = [p for p in _fixture_files() if p not in _templated_fixtures()]
    assert plain, "expected at least one fixture with no placeholders"
    assert render_fixture(plain[0]) == plain[0]


def test_render_text_substitutes_each_token():
    root = str(main_repo_root())
    out = render_text("a {{REPO_ROOT}} b {{HOME}} c {{PROJECT_SLUG}} d")
    assert "{{" not in out
    assert root in out
    assert root.replace("/", "-") in out
