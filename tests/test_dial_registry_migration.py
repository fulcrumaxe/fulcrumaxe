"""tests/test_dial_registry_migration.py

Verify that dial_registry._load_registry() migrates legacy executor.spawn
key to agent.spawn on first load, persists atomically, emits one audit row,
and is idempotent (second load is a no-op).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_legacy_registry(
    state_dir: Path,
    legacy_directives: list[dict],
    agent_directives: list[dict] | None = None,
) -> None:
    """Write a dial-registry.json containing the legacy executor.spawn key."""
    registry: dict = {
        "executor.spawn": {
            "level": 4,
            "ceiling": 5,
            "directives": legacy_directives,
        },
    }
    if agent_directives is not None:
        registry["agent.spawn"] = {
            "level": 4,
            "ceiling": 5,
            "directives": agent_directives,
        }
    path = state_dir / "dial-registry.json"
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")


def _read_registry(state_dir: Path) -> dict:
    path = state_dir / "dial-registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _read_audit_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "audit.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Isolated state dir; patch dial_registry to use it."""
    d = tmp_path / "state"
    d.mkdir()

    import backend.dial_registry as dr
    monkeypatch.setattr(dr, "_state_dir", lambda: d)
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_legacy_executor_spawn_migration(state_dir):
    """
    First load: executor.spawn directives are merged into agent.spawn,
    executor.spawn key is removed from disk, one audit row emitted.
    """
    legacy_dir = {"level": 3, "source": {"kind": "system", "reason": "test"}, "set_at": "2026-01-01T00:00:00+00:00", "ttl_until": None}
    _make_legacy_registry(state_dir, legacy_directives=[legacy_dir])

    import backend.dial_registry as dr
    registry = dr._load_registry()

    # executor.spawn must be gone from memory
    assert "executor.spawn" not in registry, "executor.spawn still in registry after migration"
    # agent.spawn must exist with the migrated directive
    assert "agent.spawn" in registry
    directives = registry["agent.spawn"]["directives"]
    assert len(directives) == 1, f"Expected 1 directive, got {len(directives)}: {directives}"
    assert directives[0] == legacy_dir

    # Disk file must also not contain executor.spawn
    on_disk = _read_registry(state_dir)
    assert "executor.spawn" not in on_disk
    assert "agent.spawn" in on_disk
    assert len(on_disk["agent.spawn"]["directives"]) == 1

    # One audit row with correct fields
    rows = _read_audit_rows(state_dir)
    migration_rows = [r for r in rows if r.get("kind") == "dial_state_migration"]
    assert len(migration_rows) == 1, f"Expected 1 migration audit row, got {len(migration_rows)}"
    mr = migration_rows[0]
    assert mr["directives_moved"] == 1
    assert mr["legacy_class"] == "executor.spawn"
    assert mr["target_class"] == "agent.spawn"


def test_migration_is_idempotent(state_dir):
    """Second load after migration must not rewrite the file or emit another audit row."""
    legacy_dir = {"level": 2, "source": {"kind": "system", "reason": "test"}, "set_at": "2026-01-01T00:00:00+00:00", "ttl_until": None}
    _make_legacy_registry(state_dir, legacy_directives=[legacy_dir])

    import backend.dial_registry as dr

    # First load: migration happens
    dr._load_registry()

    mtime_after_first = (state_dir / "dial-registry.json").stat().st_mtime
    audit_rows_after_first = _read_audit_rows(state_dir)

    # Second load: no migration
    dr._load_registry()

    mtime_after_second = (state_dir / "dial-registry.json").stat().st_mtime
    audit_rows_after_second = _read_audit_rows(state_dir)

    assert mtime_after_second == mtime_after_first, "Registry file was rewritten on second load"
    migration_rows = [r for r in audit_rows_after_second if r.get("kind") == "dial_state_migration"]
    assert len(migration_rows) == 1, f"Expected only 1 migration row, got {len(migration_rows)}"


def test_migration_concat_order_legacy_first(state_dir):
    """Legacy directives must be prepended before existing agent.spawn directives."""
    legacy_dir = {"level": 3, "source": {"kind": "system", "reason": "legacy"}, "set_at": "2026-01-01T00:00:00+00:00", "ttl_until": None}
    existing_dir = {"level": 4, "source": {"kind": "system", "reason": "existing"}, "set_at": "2026-02-01T00:00:00+00:00", "ttl_until": None}
    _make_legacy_registry(state_dir, legacy_directives=[legacy_dir], agent_directives=[existing_dir])

    import backend.dial_registry as dr
    registry = dr._load_registry()

    directives = registry["agent.spawn"]["directives"]
    assert len(directives) == 2
    assert directives[0] == legacy_dir, "Legacy directive must come first"
    assert directives[1] == existing_dir, "Existing directive must come second"


def test_migration_zero_legacy_directives(state_dir):
    """executor.spawn with empty directives list still triggers migration + audit row."""
    _make_legacy_registry(state_dir, legacy_directives=[])

    import backend.dial_registry as dr
    registry = dr._load_registry()

    assert "executor.spawn" not in registry
    # audit row emitted even when 0 directives moved
    rows = _read_audit_rows(state_dir)
    migration_rows = [r for r in rows if r.get("kind") == "dial_state_migration"]
    assert len(migration_rows) == 1
    assert migration_rows[0]["directives_moved"] == 0


def _make_full_legacy_registry(state_dir: Path) -> None:
    """Write a realistic dial-registry.json with all 13 classes, using legacy executor.spawn."""
    import backend.dial_registry as dr
    # Build a full registry using defaults, but replace agent.spawn with executor.spawn
    registry = {}
    for entry in dr._DEFAULT_DIALS:
        cls = entry["class"]
        stored_cls = "executor.spawn" if cls == "agent.spawn" else cls
        registry[stored_cls] = {
            "level": entry["level"],
            "ceiling": entry["ceiling"],
            "directives": [],
        }
    path = state_dir / "dial-registry.json"
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")


def test_class_count_after_migration(state_dir):
    """list_directives() returns exactly 13 classes after migration."""
    _make_full_legacy_registry(state_dir)

    import backend.dial_registry as dr
    directives = dr.list_directives()
    class_names = [d["class"] for d in directives]

    assert len(class_names) == 13, f"Expected 13 classes, got {len(class_names)}: {class_names}"
    assert "executor.spawn" not in class_names
    assert "agent.spawn" in class_names
