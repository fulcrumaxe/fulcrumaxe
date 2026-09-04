"""
Control plane — runtime configuration, feature gates, and agent policies.

Reads/writes .autonomous-team/config.json under the `gates`, `policies`,
and `audit_log` keys. All changes are recorded in the audit log so every
behavioral modification is traceable.

Usage (CLI):
    python backend/control_plane.py show
    python backend/control_plane.py get gates.auto_merge
    python backend/control_plane.py set gates.auto_merge false
    python backend/control_plane.py gates
    python backend/control_plane.py audit
    python backend/control_plane.py mode show
    python backend/control_plane.py mode set strict
    python backend/control_plane.py mode list

Usage (library):
    from backend.control_plane import ControlPlane, check_gate, check_policy
    cp = ControlPlane()
    cp.load()
    if cp.gate_enabled("auto_merge"):
        ...
    policy = cp.get_policy("executor")
    cp.set("gates.auto_merge", False)
    cp.apply_mode("strict")

    # One-liner agent helpers (load fresh config each call):
    if check_gate("auto_merge"):
        ...
    ceiling = check_policy("executor", "token_ceiling")
"""

import argparse
import copy
import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.dial_cli import cmd_dials as _cmd_dials  # noqa: E402

_CONFIG_PATH = Path(".autonomous-team/config.json")

# Per-gate type metadata.  Most gates are plain booleans; entries here declare
# exceptions.  The CLI `set` command validates against this before writing so
# callers get a clear error instead of a silent schema-mismatch warning.
#   "bool"        -- standard on/off gate (default when not listed)
#   "enum[a|b|c]" -- string gate; value must be one of the listed options
_GATE_TYPES: dict[str, str] = {
    "self_observe_enforcement": "enum[shadow|advisory|enforced]",
}

_DEFAULT_GATES: dict[str, bool | str] = {
    "auto_merge": True,
    "security_review": True,
    "budget_check": True,
    "idea_generation": True,
    "stall_detection": True,
    "wiki_sync": True,
    # Scheduled-jobs dispatcher off-switch (D#2046). Declared explicitly rather
    # than left implicit so `get gates.scheduled_jobs` always succeeds (exit 0)
    # instead of relying on the dispatcher's own `|| echo "false"` shell
    # fallback to fail closed on a missing key.
    "scheduled_jobs": False,
    "human_verification": False,
    # Self-observe gate: agents scan own transcript before verdict:done.
    # Default false = shadow mode (writes retros but does NOT flip verdict to needs-fix).
    # Flip to true after 24h shadow-mode data shows FP-rate <5%.
    "self_observe_executor": False,
    "self_observe_impl_coord": False,
    # Self-observe enforcement mode (string gate — only string value in _DEFAULT_GATES).
    # Controls what post-agent-hook.sh does when self_observed is missing from an envelope.
    #   "shadow"   — no-op, current production behavior (default)
    #   "advisory" — emit a team-log WARN line when a done/pass agent skipped self-observe
    #   "enforced" — advisory warning PLUS verdict downgrade to needs-fix (not yet wired)
    # Flip to "advisory" after this PR merges to start surfacing violation counts.
    "self_observe_enforcement": "shadow",
    # Docs-writer gate: spawn docs-writer alongside code-reviewer when PR touches user-facing surfaces.
    # Set false to skip docs-writer spawns entirely (useful during high-throughput periods).
    "docs_writer": True,
    # Incident-commander gate: spawn incident-commander at /loop step 5.0.5 when detector fires.
    # Default false — opt-in until detector is calibrated and we trust it not to false-positive.
    "incident_commander": False,
    # Release-manager gate: spawn release-manager after PR merges touching high-risk paths.
    # Default true — opt-out to skip release artifact authoring.
    "release_manager": True,
    # Runbook-writer gate: spawn runbook-writer after high-risk releases (release-manager risk=high).
    # Default true — opt-out to skip runbook authoring during high-throughput periods.
    "runbook_writer": True,
    # Analytics-engineer gate: emit DORA + KPI snapshots to wiki/analytics/.
    # Default true — set false to skip snapshot generation during high-throughput periods.
    "analytics_engineer": True,
    # Phased orchestration gates (D#559).
    # phased_orchestration defaults false — master switch, flip after >=10 phased merges (PR-e).
    # phased_code_review defaults true — activated in PR-c of D#559.
    # See "Phased Orchestration Gates" in CLAUDE.md for the full gate matrix.
    "phased_orchestration": False,
    "phased_code_review": True,
    # Cost-aware Discussion router (D#836).
    # When true, Team Lead consults scripts/lib/route_discussion_wiring.py before spawning.
    # When false, falls back to today's static label-based mapping.
    "cost_aware_router": False,
    # Debater pass (D#841): adversarial second pass after code-reviewer pass verdict.
    # Default false — flip after 30-day replay precision data shows ≥30% substantive flags.
    "debater_pass": False,
    # TUI tester pilot sweep (D#855): cron-driven sweep that files Discussions on failures.
    # Default false — opt-in; enable after confirming sweep runs cleanly in dry-run mode.
    "tui_tester_pilot_sweep": False,
    # Execve fence (D#887): kernel-level claude spawn block via seccomp NOTIFY filter.
    # Default true — fence is active on every subagent spawn.
    # Set false to skip fence for debugging or on kernels < 5.6 (pidfd_getfd).
    "execve_fence": True,
    # Loop-start gate (D#505): dashboard's loop.start RPC is disabled by default.
    # CLI (Team Lead + cron) is the sole loop spawner. Dashboard is read/stop only.
    # Set true ONLY for intentional operator testing — never in normal operation.
    "loop_start": False,
    # Dial-state-summary scheduled job (D#1188): daily 07:00 UTC snapshot of all dial classes.
    # Posts one-line summary to team log; names non-default classes (level!=ceiling or directives).
    # Default false — opt-in; enable when overnight dial drift visibility is needed.
    "dial_state_summary": False,
}

_DEFAULT_SETTINGS: dict = {
    "team-lead": {
        "max_parallel_impl": 3,
    },
}

_DEFAULT_POLICIES: dict[str, dict] = {
    "executor": {
        "timeout_minutes": 45,
        "max_retries": 2,
        "token_ceiling": 500_000,
    },
    "code-reviewer": {
        "timeout_minutes": 20,
        "max_retries": 1,
        "token_ceiling": 200_000,
        "max_concurrent": 4,
    },
    "security-reviewer": {
        "timeout_minutes": 20,
        "max_retries": 1,
        "token_ceiling": 200_000,
    },
    "project-manager": {
        "timeout_minutes": 30,
        "max_retries": 1,
        "token_ceiling": 300_000,
    },
    "incident_commander": {
        "timeout_minutes": 30,
        "max_retries": 1,
        "token_ceiling": 80_000,
        "max_spawns_per_hour": 1,
    },
    # Debater (D#841): adversarial Haiku pass after code-reviewer pass.
    "debater": {
        "token_cap": 5_000,
        "timeout_seconds": 90,
        "min_precision_30d": 0.30,
    },
    # Loop-run log retention (D#412): 30-day sweep via scripts/sweep-loop-runs.sh.
    "loop_runs": {
        "retention_days": 30,
    },
    # Worktree claim gate staleness (D#1819): wall-clock companion to the
    # existing policies.team_lead.claim_gate_stale_commits (default 20,
    # unchanged — not redefined here). See scripts/lib/worktree-claims.sh.
    #
    # claim_gate_abandoned_hours (D#2155, PR-a): a worktree abandoned BEFORE
    # it ever produces a PR has no terminal state under MERGED/STALE — both
    # rely on signals that only exist once a PR opens or enough days pass. A
    # working default, live on every call, by design (same as
    # claim_gate_stale_days) — NOT an opt-in boolean. D#1819's
    # enable_git_tracked_removal reached 0/192 precisely because an
    # opt-in-off boolean is dead on arrival; a threshold with a shipped
    # default is what actually gets exercised.
    "team_lead": {
        "claim_gate_stale_days": 14,
        "claim_gate_abandoned_hours": 24,
    },
}

_MODE_PRESETS: dict[str, dict] = {
    "strict": {
        "gates": {
            "auto_merge": False,
            "security_review": True,
            "budget_check": True,
            "idea_generation": True,
            "stall_detection": True,
            "wiki_sync": True,
        },
        "policies": {
            "executor": {"max_retries": 1, "token_ceiling": 300_000},
            "code-reviewer": {"timeout_minutes": 30},
        },
        "settings": {
            "team-lead": {"max_parallel_impl": 1},
        },
    },
    "standard": {
        "gates": dict(_DEFAULT_GATES),
        "policies": {},
        "settings": {},
    },
    "fast": {
        "gates": {
            "auto_merge": True,
            "security_review": False,
            "budget_check": False,
            "idea_generation": True,
            "stall_detection": True,
            "wiki_sync": False,
        },
        "policies": {
            "executor": {"max_retries": 3, "token_ceiling": 800_000, "timeout_minutes": 60},
            "code-reviewer": {"timeout_minutes": 10},
        },
        "settings": {
            "team-lead": {"max_parallel_impl": 5},
        },
    },
    "readonly": {
        "gates": {
            "auto_merge": False,
            "security_review": True,
            "budget_check": True,
            "idea_generation": False,
            "stall_detection": False,
            "wiki_sync": False,
        },
        "policies": {
            "executor": {"max_retries": 0, "token_ceiling": 0},
        },
        "settings": {
            "team-lead": {"max_parallel_impl": 0},
        },
    },
}


# ---------------------------------------------------------------------------
# Dials schema
# ---------------------------------------------------------------------------

# Hardcoded ceilings for sensitive dial classes (mirrors dial_registry._CEILINGS).
# Kept here so control_plane can validate/display dial config without importing
# dial_registry (which reads external state files).
_DIAL_CEILINGS: dict[str, int] = {
    "sandbox.modify": 1,
    "methodology.change": 2,
    "external.system": 2,
}
_DIAL_DEFAULT_CEILING = 5

# Default dial classes registered in the system.
# Schema per entry: {class_name: str, level: int, ceiling: int,
#                    directives: [{level, source, set_at, ttl_until|null}]}
_DEFAULT_DIALS: list[dict] = [
    {"class_name": "docs.write",         "level": 5, "ceiling": 5},
    {"class_name": "tests.add",          "level": 4, "ceiling": 5},
    {"class_name": "deps.bump",          "level": 3, "ceiling": 5},
    {"class_name": "agent.spawn",         "level": 4, "ceiling": 5},
    {"class_name": "merge.standard",      "level": 4, "ceiling": 5},
    {"class_name": "merge.fast-path",     "level": 2, "ceiling": 5},
    {"class_name": "intent.generate",     "level": 1, "ceiling": 5},
    {"class_name": "methodology.change",  "level": 1, "ceiling": 2},
    {"class_name": "external.system",     "level": 1, "ceiling": 2},
    {"class_name": "sandbox.modify",      "level": 1, "ceiling": 1},
    {"class_name": "cost.spend",          "level": 2, "ceiling": 5},
    {"class_name": "memory.write",        "level": 3, "ceiling": 5},
    {"class_name": "archive.move",        "level": 4, "ceiling": 5},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_config_path() -> Path:
    # Allow test override via AF_CONTROL_PLANE_CONFIG env var
    import os
    override = os.environ.get("AF_CONTROL_PLANE_CONFIG")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    return repo_root / _CONFIG_PATH


class ControlPlane:
    """
    Runtime configuration, feature gates, and agent policies.

    State is persisted in .autonomous-team/config.json. All writes are
    atomic (flock + temp-file swap) and append an audit log entry.
    """

    def __init__(self, config_path: Path | None = None):
        self._path = config_path or _resolve_config_path()
        self._data: dict = {}

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Read config.json and populate internal state with defaults for missing keys."""
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._data = {}

        # Inject defaults for sections that may not exist yet
        if "gates" not in self._data:
            self._data["gates"] = dict(_DEFAULT_GATES)
        else:
            for k, v in _DEFAULT_GATES.items():
                self._data["gates"].setdefault(k, v)

        if "policies" not in self._data:
            self._data["policies"] = {k: dict(v) for k, v in _DEFAULT_POLICIES.items()}
        else:
            for role, defaults in _DEFAULT_POLICIES.items():
                self._data["policies"].setdefault(role, {})
                for k, v in defaults.items():
                    self._data["policies"][role].setdefault(k, v)

        if "settings" not in self._data:
            self._data["settings"] = {k: dict(v) for k, v in _DEFAULT_SETTINGS.items()}
        else:
            for section, defaults in _DEFAULT_SETTINGS.items():
                self._data["settings"].setdefault(section, {})
                for k, v in defaults.items():
                    self._data["settings"][section].setdefault(k, v)

        if "audit_log" not in self._data:
            self._data["audit_log"] = []

        # Inject dials section with defaults for missing classes
        if "dials" not in self._data:
            self._data["dials"] = {
                d["class_name"]: {
                    "level": d["level"],
                    "ceiling": _DIAL_CEILINGS.get(d["class_name"], d["ceiling"]),
                    "directives": [],
                }
                for d in _DEFAULT_DIALS
            }
        else:
            for d in _DEFAULT_DIALS:
                cls = d["class_name"]
                self._data["dials"].setdefault(cls, {
                    "level": d["level"],
                    "ceiling": _DIAL_CEILINGS.get(cls, d["ceiling"]),
                    "directives": [],
                })
                # Always enforce hardcoded ceiling
                if cls in _DIAL_CEILINGS:
                    self._data["dials"][cls]["ceiling"] = _DIAL_CEILINGS[cls]

    def save(self) -> None:
        """Atomic write: flock the file, write to a temp file, then rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")

        with self._path.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                tmp_path.write_text(
                    json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                tmp_path.rename(self._path)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Get / Set with dot-notation
    # ------------------------------------------------------------------

    def get(self, key: str):
        """
        Retrieve a value using dot-notation (e.g. 'gates.auto_merge').

        For keys under the 'dials' section, dial class names may contain dots
        (e.g. 'agent.spawn', 'merge.fast-path').  Naive splitting on '.' would
        break these names.  When the first segment is 'dials', this method uses
        longest-prefix matching against the registered class names so that a key
        like 'dials.agent.spawn.level' resolves correctly.

        Returns None if any segment is missing.
        """
        parts = key.split(".")

        # Fast path for non-dials keys — no dotted class names to worry about.
        if not parts or parts[0] != "dials":
            node = self._data
            for part in parts:
                if not isinstance(node, dict) or part not in node:
                    return None
                node = node[part]
            return node

        # 'dials' section: after stripping the leading 'dials' segment, find the
        # longest registered class name that is a prefix of the remaining path.
        dials_node = self._data.get("dials")
        if not isinstance(dials_node, dict):
            return None

        remainder = ".".join(parts[1:])  # e.g. "agent.spawn.level"
        if not remainder:
            return dials_node

        # Build sorted list of class names (longest first) for prefix matching.
        known_classes = sorted(dials_node.keys(), key=len, reverse=True)
        matched_class = None
        for cls in known_classes:
            # Exact match: remainder IS the class name
            if remainder == cls:
                matched_class = cls
                break
            # Prefix match: remainder starts with "<class_name>."
            if remainder.startswith(cls + "."):
                matched_class = cls
                break

        if matched_class is None:
            return None

        node = dials_node[matched_class]
        # Remaining path after the class name
        suffix = remainder[len(matched_class):]  # "" or ".field.subfield"
        if not suffix:
            return node
        # suffix starts with '.', strip it
        for part in suffix.lstrip(".").split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set(self, key: str, value) -> None:
        """
        Set a value using dot-notation and record an audit log entry.

        Creates intermediate dicts as needed. Validates the resulting config
        against the config schema and logs a warning if invalid.
        """
        import logging as _logging  # noqa: PLC0415
        _cp_logger = _logging.getLogger(__name__)

        old_value = self.get(key)
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

        # Append audit entry (keep last 200 entries)
        entry = {
            "timestamp": _now_iso(),
            "key": key,
            "old_value": old_value,
            "new_value": value,
        }
        self._data.setdefault("audit_log", []).append(entry)
        self._data["audit_log"] = self._data["audit_log"][-200:]

        # Validate the new config state — log warnings, never block.
        try:
            from backend.schema_validator import SchemaValidator  # noqa: PLC0415
            sv = SchemaValidator()
            # Validate a copy of the data without the audit_log (not in schema)
            data_for_validation = {k: v for k, v in self._data.items() if k != "audit_log"}
            errors = sv.validate(data_for_validation, "config")
            if errors:
                for err in errors:
                    _cp_logger.warning("config validation after set(%s): %s", key, err)
                try:
                    from backend.event_bus import ConfigValidationEvent, get_bus  # noqa: PLC0415
                    get_bus().publish(ConfigValidationEvent(
                        source="control_plane",
                        file_name="config.json",
                        errors=errors,
                    ))
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            _cp_logger.debug("schema validation skipped: %s", exc)

        self.save()

        # Emit to centralized audit trail only on actual mutations (best-effort — never raises).
        # Skip when old_value == new_value so no-op writes (e.g. setting a key to its current
        # value) don't produce duplicate audit rows.
        #
        # NOTE: We construct AuditTrail directly from self._path.parent rather than calling
        # get_audit_trail(), because get_audit_trail() is a singleton that resolves to the
        # production state dir. During tests, the ControlPlane instance is backed by a
        # tmp_path config, but the singleton would still write to the real state dir's
        # audit log.  Using the instance's own config dir keeps audit writes co-located
        # with the config file and fully isolated in tests.
        if old_value != value:
            try:
                from backend.audit_trail import AuditTrail  # noqa: PLC0415
                AuditTrail(self._path.parent / "audit.jsonl").emit(
                    "control_plane", "set", key, old_value, value, "control-plane"
                )
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Feature gates
    # ------------------------------------------------------------------

    def gate_enabled(self, gate_name: str) -> bool:
        """Return True if the named gate is on (defaults to True if not configured)."""
        return bool(self.get(f"gates.{gate_name}") or False)

    def list_gates(self) -> dict[str, bool | str]:
        """Return all gates as a {name: bool | str} dict.

        Most gates are booleans, but string-valued gates (e.g. self_observe_enforcement)
        are returned as-is so callers can inspect their exact string value.
        """
        gates = self._data.get("gates", {})
        # Merge with defaults so new default gates always appear
        result: dict[str, bool | str] = dict(_DEFAULT_GATES)
        result.update(gates)
        # Preserve string gates; coerce all others to bool
        return {
            k: v if isinstance(v, str) else bool(v)
            for k, v in result.items()
        }

    # ------------------------------------------------------------------
    # Agent policies
    # ------------------------------------------------------------------

    def get_policy(self, role: str) -> dict:
        """
        Return the policy dict for the given agent role.

        Falls back to the hardcoded defaults if the role is not in config.
        """
        policies = self._data.get("policies", {})
        policy = policies.get(role, {})
        defaults = _DEFAULT_POLICIES.get(role, {})
        merged = dict(defaults)
        merged.update(policy)
        return merged

    # ------------------------------------------------------------------
    # Settings (numeric / non-boolean configuration)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Dial registry integration
    # ------------------------------------------------------------------

    def get_dial(self, class_name: str) -> dict | None:
        """Return the dial state dict for *class_name*, or None if unknown."""
        return self._data.get("dials", {}).get(class_name)

    def list_dials(self) -> dict[str, dict]:
        """Return all dial states keyed by class name."""
        return dict(self._data.get("dials", {}))

    def get_dial_ceiling(self, class_name: str) -> int:
        """Return the effective ceiling for *class_name* (hardcoded wins over stored)."""
        return _DIAL_CEILINGS.get(class_name, _DIAL_DEFAULT_CEILING)

    # ------------------------------------------------------------------
    # Settings (numeric / non-boolean configuration)
    # ------------------------------------------------------------------

    def get_setting(self, section: str, key: str):
        """Return a setting value from the settings section, falling back to defaults."""
        val = self.get(f"settings.{section}.{key}")
        if val is None:
            return _DEFAULT_SETTINGS.get(section, {}).get(key)
        return val

    def list_settings(self) -> dict[str, dict]:
        """Return all settings merged with defaults."""
        result: dict[str, dict] = {}
        for section, defaults in _DEFAULT_SETTINGS.items():
            merged = dict(defaults)
            stored = self._data.get("settings", {}).get(section, {})
            merged.update(stored)
            result[section] = merged
        return result

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def get_audit_log(self, limit: int = 20) -> list[dict]:
        """Return the most recent *limit* audit entries, newest first."""
        entries = self._data.get("audit_log", [])
        return list(reversed(entries[-limit:]))

    # ------------------------------------------------------------------
    # Mode presets
    # ------------------------------------------------------------------

    def apply_mode(self, mode_name: str) -> None:
        """
        Apply a named mode preset.

        Deep-merges the preset values into the current config: only keys
        explicitly listed in the preset are updated, all other keys keep
        their current values. Records an audit log entry and saves.

        Raises ValueError for unknown mode names.
        """
        if mode_name not in _MODE_PRESETS:
            raise ValueError(
                f"Unknown mode {mode_name!r}. Valid modes: {sorted(_MODE_PRESETS)}"
            )

        preset = copy.deepcopy(_MODE_PRESETS[mode_name])
        old_mode = self._data.get("active_mode")

        # Deep-merge: update only the keys listed in the preset
        for section in ("gates", "policies", "settings"):
            preset_section = preset.get(section, {})
            if not preset_section:
                continue
            current_section = self._data.setdefault(section, {})
            for key, value in preset_section.items():
                if isinstance(value, dict):
                    current_section.setdefault(key, {}).update(value)
                else:
                    current_section[key] = value

        self._data["active_mode"] = mode_name

        # Audit log entry
        entry = {
            "timestamp": _now_iso(),
            "key": "mode",
            "old_value": old_mode,
            "new_value": mode_name,
        }
        self._data.setdefault("audit_log", []).append(entry)
        self._data["audit_log"] = self._data["audit_log"][-200:]

        self.save()

    def get_mode(self) -> str | None:
        """Return the currently active mode name, or None if no mode has been applied."""
        return self._data.get("active_mode")

    def list_modes(self) -> dict[str, dict]:
        """Return all preset definitions keyed by mode name (deep copy — safe to mutate)."""
        return copy.deepcopy(_MODE_PRESETS)


# ------------------------------------------------------------------
# Module-level convenience functions for agent use
# ------------------------------------------------------------------


def check_gate(gate_name: str) -> bool:
    """Quick check: load control plane, return whether gate is enabled."""
    cp = ControlPlane()
    cp.load()
    return cp.gate_enabled(gate_name)


def check_policy(role: str, key: str):
    """Quick check: load control plane, return policy value for role."""
    cp = ControlPlane()
    cp.load()
    return cp.get_policy(role).get(key)


# ------------------------------------------------------------------
# CLI helpers
# ------------------------------------------------------------------


def _coerce_value(raw: str):
    """Try to parse raw as JSON (handles true/false/null/numbers), else return as string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _validate_gate_value(gate_name: str, value: object) -> "str | None":
    """Return an error message when *value* is invalid for *gate_name*, else None.

    Gates not listed in _GATE_TYPES are expected to be bool.
    """
    gate_type = _GATE_TYPES.get(gate_name, "bool")
    if gate_type == "bool":
        if not isinstance(value, bool):
            actual = type(value).__name__
            return f"gate '{gate_name}' expects a bool (true/false), got {actual} {value!r}"
    elif gate_type.startswith("enum[") and gate_type.endswith("]"):
        allowed = gate_type[5:-1].split("|")
        if value not in allowed:
            return f"gate '{gate_name}' expects one of {allowed!r}, got {value!r}"
    return None


def _cmd_show(cp: ControlPlane, _args) -> int:
    # Print everything except the audit_log (shown via audit subcommand)
    display = {k: v for k, v in cp._data.items() if k != "audit_log"}
    print(json.dumps(display, indent=2))
    return 0


def _cmd_get(cp: ControlPlane, args) -> int:
    value = cp.get(args.key)
    if value is None:
        print("(not set)", file=sys.stderr)
        return 1
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2))
    else:
        print(json.dumps(value))
    return 0


def _cmd_set(cp: ControlPlane, args) -> int:
    value = _coerce_value(args.value)
    # Validate gate values before writing -- gives a clear error instead of a warning.
    parts = args.key.split(".")
    if len(parts) == 2 and parts[0] == "gates":
        err = _validate_gate_value(parts[1], value)
        if err is not None:
            print(f"error: {err}", file=sys.stderr)
            return 1
    cp.set(args.key, value)
    print(f"set {args.key} = {json.dumps(value)}")
    return 0


def _cmd_gates(cp: ControlPlane, _args) -> int:
    gates = cp.list_gates()
    max_name = max((len(n) for n in gates), default=10)
    for name, enabled in sorted(gates.items()):
        status = "on " if enabled else "off"
        print(f"  {name:<{max_name}}  {status}")
    return 0


def _cmd_settings(cp: ControlPlane, _args) -> int:
    settings = cp.list_settings()
    for section, values in sorted(settings.items()):
        print(f"  [{section}]")
        for key, val in sorted(values.items()):
            print(f"    {key} = {json.dumps(val)}")
    return 0


def _cmd_audit(cp: ControlPlane, _args) -> int:
    entries = cp.get_audit_log(limit=20)
    if not entries:
        print("(no audit entries)")
        return 0
    for entry in entries:
        ts = entry.get("timestamp", "?")
        key = entry.get("key", "?")
        old = json.dumps(entry.get("old_value"))
        new = json.dumps(entry.get("new_value"))
        print(f"  {ts}  {key}: {old} \u2192 {new}")
    return 0


def _cmd_mode(cp: ControlPlane, args) -> int:
    sub = args.mode_subcommand
    if sub == "show":
        active = cp.get_mode()
        if active is None:
            print("No active mode set (using defaults)")
            return 0
        print(f"Active mode: {active}")
        preset = _MODE_PRESETS.get(active, {})
        print(json.dumps(preset, indent=2))
        return 0
    elif sub == "set":
        try:
            cp.apply_mode(args.mode_name)
            print(f"Applied mode: {args.mode_name}")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    elif sub == "list":
        for name, preset in sorted(_MODE_PRESETS.items()):
            gates_summary = ", ".join(
                f"{k}={'on' if v else 'off'}"
                for k, v in preset.get("gates", {}).items()
            )
            print(f"  {name}")
            if gates_summary:
                print(f"    gates: {gates_summary}")
            policies = preset.get("policies", {})
            if policies:
                print(f"    policies: {json.dumps(policies)}")
            settings = preset.get("settings", {})
            if settings:
                print(f"    settings: {json.dumps(settings)}")
        return 0
    else:
        print(f"Unknown mode subcommand: {sub}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="control_plane",
        description="Control plane: runtime config, feature gates, agent policies.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="Print full config (excluding audit log)")

    get_p = sub.add_parser("get", help="Get a single value by dot-notation key")
    get_p.add_argument("key", help="Dot-notation key, e.g. gates.auto_merge")

    set_p = sub.add_parser("set", help="Set a value (with audit log entry)")
    set_p.add_argument("key", help="Dot-notation key, e.g. gates.auto_merge")
    set_p.add_argument("value", help="New value (JSON-parsed: true/false/null/number/string)")

    sub.add_parser("gates", help="List all feature gates with on/off status")

    sub.add_parser("dials", help="List all autonomy dial classes with level/ceiling")

    sub.add_parser("settings", help="List all numeric/non-boolean settings")

    sub.add_parser("audit", help="Show recent config changes")

    mode_p = sub.add_parser("mode", help="Manage mode presets (strict/standard/fast/readonly)")
    mode_sub = mode_p.add_subparsers(dest="mode_subcommand", required=True)
    mode_sub.add_parser("show", help="Print current active mode and its configuration")
    mode_sub.add_parser("list", help="List all available presets with their configurations")
    mode_set_p = mode_sub.add_parser("set", help="Apply a mode preset")
    mode_set_p.add_argument("mode_name", help="Mode name: strict, standard, fast, or readonly")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    cp = ControlPlane()
    cp.load()

    dispatch = {
        "show": _cmd_show,
        "get": _cmd_get,
        "set": _cmd_set,
        "gates": _cmd_gates,
        "dials": _cmd_dials,
        "settings": _cmd_settings,
        "audit": _cmd_audit,
        "mode": _cmd_mode,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(cp, args)


if __name__ == "__main__":
    sys.exit(main())
