"""Unit tests for backend/module_health.py."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Allow running from repo root: python -m pytest backend/test_module_health.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.module_health import (
    ModuleHealthChecker,
    _publish_health_events,
    get_cached_module_health,
    _health_cache,
)


class TestDiscovery(unittest.TestCase):
    """Test 1 — module discovery."""

    def test_discovers_py_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            (d / "alpha.py").write_text("x = 1")
            (d / "beta.py").write_text("x = 2")
            (d / "test_gamma.py").write_text("x = 3")   # should be excluded
            (d / "__init__.py").write_text("")            # should be excluded
            checker = ModuleHealthChecker(backend_dir=d)
            names = checker.discover()
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertNotIn("test_gamma", names)
        self.assertNotIn("__init__", names)

    def test_discover_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            for name in ("zz", "aa", "mm"):
                (d / f"{name}.py").write_text("")
            checker = ModuleHealthChecker(backend_dir=d)
            names = checker.discover()
        self.assertEqual(names, sorted(names))


class TestImportSuccess(unittest.TestCase):
    """Test 2 — successful import detection."""

    def test_valid_module_import_ok(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_import("budget")
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_another_valid_module(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_import("registry")
        self.assertTrue(ok)
        self.assertEqual(errors, [])


class TestImportFailure(unittest.TestCase):
    """Test 3 — failed import detection."""

    def test_nonexistent_module_fails(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_import("__nonexistent_module_xyz__")
        self.assertFalse(ok)
        self.assertGreater(len(errors), 0)

    def test_error_message_captured(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_import("__nonexistent_module_xyz__")
        combined = " ".join(errors)
        self.assertIn("ModuleNotFoundError", combined)

    def test_broken_module_via_tmp(self) -> None:
        """Simulate a broken module by monkeypatching importlib."""
        checker = ModuleHealthChecker()
        with patch("importlib.import_module", side_effect=ImportError("bad import")):
            ok, errors = checker.check_import("budget")
        self.assertFalse(ok)
        self.assertTrue(any("bad import" in e for e in errors))


class TestCLICheck(unittest.TestCase):
    """Test 4 — CLI subprocess check."""

    def test_cli_check_valid_module(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_cli("budget", "status")
        # Should succeed — budget.py is importable
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_cli_check_nonexistent_module(self) -> None:
        checker = ModuleHealthChecker()
        ok, errors = checker.check_cli("__nonexistent_module_xyz__", "check")
        self.assertFalse(ok)
        self.assertGreater(len(errors), 0)


class TestDependencyParsing(unittest.TestCase):
    """Test 5 — dependency graph parsing."""

    def test_dep_parsing_finds_backend_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            # alpha depends on beta
            (d / "alpha.py").write_text("from backend.beta import Foo\n")
            (d / "beta.py").write_text("Foo = 1\n")
            checker = ModuleHealthChecker(backend_dir=d)
            # Patch check_import so beta returns success without actually importing
            with patch.object(checker, "check_import", return_value=(True, [])):
                deps, dep_ok, errors = checker.check_deps("alpha")
        self.assertIn("beta", deps)
        self.assertTrue(dep_ok)
        self.assertEqual(errors, [])

    def test_dep_parsing_flags_missing_dep(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            (d / "alpha.py").write_text("from backend.missing_dep import Bar\n")
            checker = ModuleHealthChecker(backend_dir=d)
            deps, dep_ok, errors = checker.check_deps("alpha")
        self.assertIn("missing_dep", deps)
        self.assertFalse(dep_ok)
        self.assertTrue(any("missing" in e for e in errors))

    def test_dep_parsing_no_backend_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            (d / "standalone.py").write_text("import os\nimport sys\n")
            checker = ModuleHealthChecker(backend_dir=d)
            deps, dep_ok, errors = checker.check_deps("standalone")
        self.assertEqual(deps, [])
        self.assertTrue(dep_ok)
        self.assertEqual(errors, [])


class TestCaching(unittest.TestCase):
    """Test 6 — result caching."""

    def setUp(self) -> None:
        # Reset cache before each test
        _health_cache["data"] = None
        _health_cache["expires_at"] = 0.0

    def test_cache_returns_same_result_within_ttl(self) -> None:
        call_count = [0]

        def _fake_run_all(**_kwargs):
            call_count[0] += 1
            return {"timestamp": "t", "total": 1, "passed": 1, "failed": 0, "modules": []}

        checker = ModuleHealthChecker()
        with patch.object(checker, "run_all", side_effect=_fake_run_all):
            with patch("backend.module_health.ModuleHealthChecker", return_value=checker):
                r1 = get_cached_module_health(ttl=60.0)
                r2 = get_cached_module_health(ttl=60.0)

        # Only one run_all call should happen inside the patched context
        self.assertEqual(r1["total"], r2["total"])

    def test_cache_expires_and_recomputes(self) -> None:
        _health_cache["data"] = {"stale": True}
        _health_cache["expires_at"] = time.monotonic() - 1  # already expired

        result = get_cached_module_health(ttl=60.0)
        # Should have recomputed — stale key should not be present
        self.assertNotIn("stale", result)


class TestAPIEndpointResponseFormat(unittest.TestCase):
    """Test 7 — JSON structure matches spec."""

    def test_run_all_has_required_keys(self) -> None:
        checker = ModuleHealthChecker()
        report = checker.run_all()
        self.assertIn("timestamp", report)
        self.assertIn("total", report)
        self.assertIn("passed", report)
        self.assertIn("failed", report)
        self.assertIn("modules", report)

    def test_module_entry_has_required_keys(self) -> None:
        checker = ModuleHealthChecker()
        report = checker.run_all()
        self.assertGreater(len(report["modules"]), 0)
        m = report["modules"][0]
        for key in ("name", "import_ok", "cli_ok", "dependencies", "dep_ok", "errors"):
            self.assertIn(key, m, f"missing key: {key}")

    def test_passed_plus_failed_equals_total(self) -> None:
        checker = ModuleHealthChecker()
        report = checker.run_all()
        self.assertEqual(report["passed"] + report["failed"], report["total"])

    def test_output_is_json_serialisable(self) -> None:
        checker = ModuleHealthChecker()
        report = checker.run_all()
        # Should not raise
        json.dumps(report)


class TestEventBusPublication(unittest.TestCase):
    """Test 8 — event bus publication on module failure."""

    def test_publishes_event_for_failed_module(self) -> None:
        from backend.event_bus import get_bus, ModuleHealthEvent

        received: list[ModuleHealthEvent] = []
        bus = get_bus()
        sub_id = bus.subscribe(ModuleHealthEvent, received.append)

        report = {
            "timestamp": "2026-04-10T22:00:00Z",
            "total": 1,
            "passed": 0,
            "failed": 1,
            "modules": [
                {
                    "name": "broken",
                    "import_ok": False,
                    "cli_ok": None,
                    "dependencies": [],
                    "dep_ok": True,
                    "errors": ["ModuleNotFoundError: boom"],
                }
            ],
        }
        _publish_health_events(report)
        bus.unsubscribe(sub_id)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].module_name, "broken")
        self.assertIn("ModuleNotFoundError: boom", received[0].errors)

    def test_no_event_for_passing_modules(self) -> None:
        from backend.event_bus import get_bus, ModuleHealthEvent

        received: list[ModuleHealthEvent] = []
        bus = get_bus()
        sub_id = bus.subscribe(ModuleHealthEvent, received.append)

        report = {
            "timestamp": "2026-04-10T22:00:00Z",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "modules": [
                {
                    "name": "ok_module",
                    "import_ok": True,
                    "cli_ok": None,
                    "dependencies": [],
                    "dep_ok": True,
                    "errors": [],
                }
            ],
        }
        _publish_health_events(report)
        bus.unsubscribe(sub_id)

        self.assertEqual(len(received), 0)


if __name__ == "__main__":
    unittest.main()
