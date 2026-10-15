"""Tests for backend/conftest.py's optional-API-dependency skips and skip list.

backend/conftest.py downgrades a failure to a skip only when ALL of these hold:

* the failure is a `ModuleNotFoundError` (on the exception, or one level down
  its `__cause__`/`__context__` at call time) whose top-level name is exactly
  `fastapi`, `starlette` or `uvicorn`, and
* `importlib.util.find_spec(<that name>) is None` -- the package is genuinely
  not installed.

The second condition is the point of these tests: a `ModuleNotFoundError`
that names an *installed* package (a removed submodule, a hand-raised error,
a wrapped cause) is a real failure and must stay one, in the collection-time
hook and in both call-time hooks.

Each scenario runs `python3 -m pytest` in a real subprocess against a scratch
directory created with `tempfile.mkdtemp()` (never inside this repo tree --
scripts/check-tests-leave-tree-clean.sh forbids that), with its own
pytest.ini (pins the rootdir, so node IDs are stable), cwd set to it, and a
`conftest.py` that re-exports the hooks from `backend.conftest`.

The scratch tests are hermetic: a package is "not installed" by setting
`sys.modules[name] = None` (the import system then raises
`ModuleNotFoundError(name=...)` and `find_spec` reports None), and "installed"
by a tiny real package of that name placed on the scratch `PYTHONPATH`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_SCRATCH_CONFTEST = (
    "from backend.conftest import (  # noqa: F401\n"
    "    pytest_pycollect_makemodule,\n"
    "    pytest_runtest_setup,\n"
    "    pytest_runtest_call,\n"
    "    pytest_collection_modifyitems,\n"
    "    reset_limiter_state,\n"
    ")\n"
)

# A real, importable package named "uvicorn" (one of the optional names),
# so find_spec("uvicorn") is not None in the scratch run.
_FAKE_UVICORN = {"uvicorn/__init__.py": "VALUE = 1\n"}

# Prelude making fastapi look uninstalled.
_NO_FASTAPI = "import sys\n\nsys.modules['fastapi'] = None\n"


def _run(
    files: dict[str, str],
    args: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    scratch = Path(tempfile.mkdtemp(prefix="optional-api-deps-"))
    (scratch / "pytest.ini").write_text("[pytest]\n")
    (scratch / "conftest.py").write_text(_SCRATCH_CONFTEST)
    for rel, content in files.items():
        path = scratch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    env = dict(os.environ)
    env.pop("BACKEND_TESTS_SKIPLIST_PATH", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT), str(scratch), env.get("PYTHONPATH", "")]
    )
    env.update(env_extra or {})

    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"]
        + (args or ["."]),
        capture_output=True,
        text=True,
        env=env,
        cwd=scratch,
        timeout=60,
    )


def _out(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


# --------------------------------------------------------------------------
# Collection-time hook
# --------------------------------------------------------------------------


def test_missing_fastapi_is_skipped_not_errored() -> None:
    """A module whose `import fastapi` fails because fastapi is truly absent
    is skipped. Exit 5, not 0: a module-level skip stops collection before any
    item exists, so pytest reports "no tests collected" -- not an error."""
    result = _run(
        {"test_m.py": _NO_FASTAPI + "import fastapi\n\n\ndef test_x():\n    pass\n"}
    )
    out = _out(result)
    assert "ERROR" not in out, out
    assert "1 skipped" in out, out
    assert "fastapi" in out and "optional" in out, out
    assert result.returncode == 5, out


def test_import_error_for_installed_package_is_a_collection_error() -> None:
    """The package exists but a submodule import raises on import: that is a
    real collection error (exit 2), never a skip."""
    result = _run(
        {
            **_FAKE_UVICORN,
            "test_m.py": "import uvicorn.no_such_submodule\n\n\ndef test_x():\n    pass\n",
        }
    )
    out = _out(result)
    assert result.returncode == 2, out
    assert "ERROR" in out, out
    assert "no_such_submodule" in out, out
    assert "skipped" not in out.lower(), out


def test_missing_unrelated_module_is_a_collection_error() -> None:
    result = _run(
        {"test_m.py": "import d40_definitely_not_installed\n\n\ndef test_x():\n    pass\n"}
    )
    out = _out(result)
    assert result.returncode == 2, out
    assert "d40_definitely_not_installed" in out, out
    assert "skipped" not in out.lower(), out


def test_module_name_prefix_match_is_a_collection_error() -> None:
    result = _run(
        {"test_m.py": _NO_FASTAPI + "import fastapi_extras_d40\n\n\ndef test_x():\n    pass\n"}
    )
    out = _out(result)
    assert result.returncode == 2, out
    assert "fastapi_extras_d40" in out, out
    assert "skipped" not in out.lower(), out


# --------------------------------------------------------------------------
# Call-time hooks (test body = pytest_runtest_call, fixture = ..._setup)
# --------------------------------------------------------------------------


def test_calltime_missing_fastapi_in_body_is_skipped() -> None:
    result = _run(
        {
            "test_m.py": _NO_FASTAPI
            + "\n\ndef test_x():\n    import fastapi  # noqa: F401\n"
        }
    )
    out = _out(result)
    assert "1 skipped" in out and "fastapi" in out, out
    assert "failed" not in out and "error" not in out.lower(), out
    assert result.returncode == 0, out


def test_calltime_missing_fastapi_in_fixture_is_skipped() -> None:
    result = _run(
        {
            "test_m.py": _NO_FASTAPI
            + "import pytest\n\n\n@pytest.fixture\ndef dep():\n"
            "    import fastapi  # noqa: F401\n\n\n"
            "def test_x(dep):\n    pass\n"
        }
    )
    out = _out(result)
    assert "1 skipped" in out and "fastapi" in out, out
    assert "failed" not in out and "error" not in out.lower(), out
    assert result.returncode == 0, out


def test_calltime_import_raising_for_installed_package_fails() -> None:
    """Module exists but raises ModuleNotFoundError on import (a removed
    submodule after a version bump): the test FAILS, it is not skipped."""
    result = _run(
        {
            **_FAKE_UVICORN,
            "test_m.py": "def test_x():\n    import uvicorn.no_such_submodule\n",
        }
    )
    out = _out(result)
    assert "1 failed" in out, out
    assert "skipped" not in out.lower(), out
    assert result.returncode == 1, out


def test_calltime_installed_package_failure_variants_fail() -> None:
    """With the package installed: a hand-raised ModuleNotFoundError, a real
    assert / pytest.fail inside an except handler (implicit __context__), and a
    wrapped __cause__ are all real failures."""
    source = '''import pytest


def test_assert_in_handler():
    try:
        raise ModuleNotFoundError("x", name="uvicorn")
    except ModuleNotFoundError:
        assert 1 == 2, "real failure"


def test_fail_in_handler():
    try:
        raise ModuleNotFoundError("x", name="uvicorn")
    except ModuleNotFoundError:
        pytest.fail("uvicorn is required for this test")


def test_wrapped_cause():
    try:
        raise ModuleNotFoundError("x", name="uvicorn")
    except ModuleNotFoundError as e:
        raise RuntimeError("server failed to boot") from e


def test_hand_raised():
    raise ModuleNotFoundError("x", name="uvicorn")
'''
    result = _run({**_FAKE_UVICORN, "test_m.py": source})
    out = _out(result)
    assert "4 failed" in out, out
    assert "skipped" not in out.lower(), out
    assert result.returncode == 1, out


def test_calltime_installed_package_in_fixture_is_an_error() -> None:
    result = _run(
        {
            **_FAKE_UVICORN,
            "test_m.py": "import pytest\n\n\n@pytest.fixture\ndef dep():\n"
            "    import uvicorn.no_such_submodule\n\n\n"
            "def test_x(dep):\n    pass\n",
        }
    )
    out = _out(result)
    assert "1 error" in out, out
    assert "skipped" not in out.lower(), out
    assert result.returncode == 1, out


def test_calltime_plain_import_error_fails() -> None:
    """`from <installed pkg> import <missing name>` is an ImportError, not a
    ModuleNotFoundError: always a failure."""
    result = _run(
        {**_FAKE_UVICORN, "test_m.py": "def test_x():\n    from uvicorn import nope\n"}
    )
    out = _out(result)
    assert "1 failed" in out, out
    assert "skipped" not in out.lower(), out


def test_calltime_other_module_names_fail_even_when_fastapi_absent() -> None:
    """Name matching stays exact: 'notfastapi' / 'fastapi_extra' are not
    'fastapi', whether or not fastapi itself is installed."""
    source = _NO_FASTAPI + '''

def test_other_name():
    raise ModuleNotFoundError("x", name="notfastapi")


def test_prefix_name():
    raise ModuleNotFoundError("x", name="fastapi_extra")
'''
    result = _run({"test_m.py": source})
    out = _out(result)
    assert "2 failed" in out, out
    assert "skipped" not in out.lower(), out


# --------------------------------------------------------------------------
# Skip-list loader
# --------------------------------------------------------------------------

_TWO_FILES = {
    "test_a.py": "def test_a():\n    assert True\n",
    "test_b.py": "def test_b():\n    assert False\n",
}


def _run_skiplist(body: str, args: list[str] | None = None) -> subprocess.CompletedProcess:
    skiplist_dir = Path(tempfile.mkdtemp(prefix="optional-api-deps-skiplist-"))
    path = skiplist_dir / "skiplist.py"
    path.write_text(body)
    return _run(
        _TWO_FILES,
        args=args,
        env_extra={"BACKEND_TESTS_SKIPLIST_PATH": str(path)},
    )


def test_skiplist_within_cap_applies_skip_marker() -> None:
    result = _run_skiplist(
        'SKIP = {"test_b.py::test_b": ("known failure", "D#43")}\n'
    )
    out = _out(result)
    assert "1 passed, 1 skipped" in out, out
    assert "known failure (tracked: D#43)" in out, out
    assert result.returncode == 0, out


def test_skiplist_over_18_ids_hard_fails() -> None:
    entries = ", ".join(f'"test_a.py::test_{i}": ("r", "D#43")' for i in range(19))
    result = _run_skiplist("SKIP = {" + entries + "}\n")
    out = _out(result)
    assert "cap of 18" in out, out
    assert result.returncode != 0, out


def test_skiplist_missing_reason_hard_fails() -> None:
    for reason in ('""', '"   "'):
        result = _run_skiplist(f'SKIP = {{"test_b.py::test_b": ({reason}, "D#43")}}\n')
        out = _out(result)
        assert "has no reason" in out, out
        assert result.returncode != 0, out


def test_skiplist_bad_discussion_ref_hard_fails() -> None:
    for ref in ('""', '"D#"', '"D#x"', '"#43"'):
        result = _run_skiplist(f'SKIP = {{"test_b.py::test_b": ("why", {ref})}}\n')
        out = _out(result)
        assert "no 'D#<n>' reference" in out, out
        assert result.returncode != 0, out


def test_skiplist_stale_id_in_collected_file_hard_fails() -> None:
    result = _run_skiplist('SKIP = {"test_b.py::test_gone": ("why", "D#43")}\n')
    out = _out(result)
    assert "match no collected test" in out, out
    assert result.returncode != 0, out


def test_skiplist_entry_for_unselected_file_does_not_break_targeted_run() -> None:
    """`pytest test_a.py` with a skip-list entry for test_b.py (a file that
    exists but was not selected) must run normally, not exit 4."""
    result = _run_skiplist(
        'SKIP = {"test_b.py::test_b": ("known failure", "D#43")}\n',
        args=["test_a.py"],
    )
    out = _out(result)
    assert "1 passed" in out, out
    assert result.returncode == 0, out


def test_skiplist_entry_for_deleted_file_hard_fails() -> None:
    result = _run_skiplist('SKIP = {"test_deleted.py::test_x": ("why", "D#43")}\n')
    out = _out(result)
    assert "match no collected test" in out, out
    assert result.returncode != 0, out
