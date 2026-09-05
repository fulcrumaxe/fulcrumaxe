"""Shared helpers for the test suites, importable from both test trees.

Deliberately NOT under tests/ or backend/tests/. `backend/` has no
__init__.py, so pytest imports `backend/tests/` under the top-level name
`tests` — the same name the repo-root `tests/` package claims. Whichever one
gets imported first wins the entry in sys.modules, which made `tests.<x>`
resolve differently depending on which files a pytest invocation selected.
A separate top-level name collides with neither.

Nothing here is collected as a test: no module in this package matches
pytest's `test_*.py` pattern.
"""
