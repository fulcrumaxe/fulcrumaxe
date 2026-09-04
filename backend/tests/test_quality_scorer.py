"""
Tests for the per-function McCabe complexity scoring in quality_scorer.py.

Covers the three new helpers (_function_complexity, _file_complexities,
and the _complexity_score method) per the spec in Discussion #304.
"""

from __future__ import annotations

import re
import textwrap

import pytest

# Import the module-level helpers under test
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.quality_scorer import (  # noqa: E402
    QualityScorer,
    _file_complexities,
    _function_complexity,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal QualityScorer that won't hit filesystem or GitHub
# ---------------------------------------------------------------------------

def _score_source(source: str) -> dict:
    """Score a single Python source string in isolation (no git, no blackboard writes)."""
    scorer = QualityScorer(repo_root="/nonexistent")
    # Build a fake diff so the scorer thinks this one file was changed
    diff = f"diff --git a/foo.py b/foo.py\n+++ b/foo.py\n"
    for line in source.splitlines():
        diff += f"+{line}\n"
    result = scorer.score_diff(diff)
    return result["breakdown"]["complexity"]


# ---------------------------------------------------------------------------
# R1 — _function_complexity unit tests
# ---------------------------------------------------------------------------

def test_function_complexity_simple() -> None:
    """A function with no branches has complexity 1."""
    src = textwrap.dedent("""\
        def foo():
            return 42
    """)
    tree = __import__("ast").parse(src)
    func = tree.body[0]
    assert _function_complexity(func) == 1


def test_function_complexity_if() -> None:
    """Each if adds 1."""
    src = textwrap.dedent("""\
        def foo(x):
            if x > 0:
                return 1
            if x < 0:
                return -1
            return 0
    """)
    tree = __import__("ast").parse(src)
    func = tree.body[0]
    assert _function_complexity(func) == 3  # 1 base + 2 ifs


def test_function_complexity_boolop() -> None:
    """A and B and C counts len(values)-1 = 2 extra branches."""
    src = textwrap.dedent("""\
        def foo(a, b, c):
            if a and b and c:
                return 1
            return 0
    """)
    tree = __import__("ast").parse(src)
    func = tree.body[0]
    # 1 base + 1 if + 2 boolop = 4
    assert _function_complexity(func) == 4


def test_function_complexity_nested_pruned() -> None:
    """Nested function branches are NOT counted in the outer function's score."""
    src = textwrap.dedent("""\
        def outer():
            def inner():
                if True:
                    pass
            return 0
    """)
    tree = __import__("ast").parse(src)
    outer = tree.body[0]
    # outer sees only base complexity 1 (inner is pruned)
    assert _function_complexity(outer) == 1


# ---------------------------------------------------------------------------
# R5 — the five spec test cases
# ---------------------------------------------------------------------------

def test_complexity_god_function_scores_low() -> None:
    """A single function with 30 ifs → cyclomatic 31 → score 0/30 (well ≤ 5)."""
    ifs = "\n".join(f"    if x > {i}:\n        pass" for i in range(30))
    src = f"def god_func(x):\n{ifs}\n    return x\n"
    result = _score_source(src)
    assert result["score"] <= 5, f"Expected ≤ 5, got {result['score']}"


def test_complexity_clean_helpers_score_high() -> None:
    """10 simple functions each with 1 decision point → cyclomatic 2 each → avg 2 → score ≥ 25.

    Formula: 30 * (1 - min(avg/8, 1)).  avg=2 → 30*(1-0.25) = 22.5 → 22... actually
    we need avg close to 1 for ≥25. Use 0-branch functions (cyclomatic 1 each) to guarantee
    score == 30, then verify ≥ 25 threshold is met comfortably.
    """
    funcs = []
    for i in range(10):
        funcs.append(
            f"def helper_{i}(x):\n"
            f"    return x + {i}\n"
        )
    src = "\n".join(funcs)
    result = _score_source(src)
    # avg complexity == 1.0 → score = 30*(1 - 1/8) = 26 (rounds to 26)
    assert result["score"] >= 25, f"Expected ≥ 25, got {result['score']}"


def test_complexity_empty_file_scores_max() -> None:
    """Empty source has no functions → score == 30 (max)."""
    result = _score_source("")
    assert result["score"] == 30, f"Expected 30, got {result['score']}"


def test_complexity_pure_pydantic_file_scores_max() -> None:
    """A file with only a BaseModel class and no real functions → score == 30."""
    src = textwrap.dedent("""\
        from pydantic import BaseModel

        class Foo(BaseModel):
            x: int
            y: str = "hello"
    """)
    result = _score_source(src)
    assert result["score"] == 30, f"Expected 30 for pydantic-only file, got {result['score']}"


def test_complexity_detail_string_format() -> None:
    """Detail string matches the required pattern when functions are present."""
    src = textwrap.dedent("""\
        def simple(x):
            if x:
                return 1
            return 0
    """)
    result = _score_source(src)
    pattern = r"^avg_func_complexity=\d+\.\d \(\d+ functions\)$"
    assert re.match(pattern, result["detail"]), (
        f"Detail string '{result['detail']}' does not match pattern '{pattern}'"
    )


# ---------------------------------------------------------------------------
# Extra: _file_complexities unit tests
# ---------------------------------------------------------------------------

def test_file_complexities_syntax_error_returns_empty() -> None:
    """SyntaxError in source → empty list, no crash."""
    assert _file_complexities("def foo(") == []


def test_file_complexities_dataclass_skipped() -> None:
    """@dataclass class bodies are excluded from complexity collection."""
    src = textwrap.dedent("""\
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: float
            y: float

            def distance(self, other):
                if other.x > 0:
                    pass
                return 0.0
    """)
    # The method inside the @dataclass class should be excluded
    result = _file_complexities(src)
    assert result == [], f"Expected no functions from dataclass body, got {result}"


def test_file_complexities_nested_counted_once() -> None:
    """Nested functions appear as separate entries, not double-counted."""
    src = textwrap.dedent("""\
        def outer(x):
            def inner(y):
                if y:
                    return 1
                return 0
            return inner(x)
    """)
    result = _file_complexities(src)
    # outer: complexity 1 (no decision points in its own body after pruning inner)
    # inner: complexity 3 (1 base + 1 if)... wait: 1 base + 1 if = 2
    assert len(result) == 2, f"Expected 2 functions, got {len(result)}"
    assert 1 in result  # outer has no branches (inner is pruned)
