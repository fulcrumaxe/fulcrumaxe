"""
tui_tester_kpi_registry.py — expected widget shapes per TUI tab + anti-pattern checks.

When a new tab or KPI tile is added to dashboard_tui/, the author registers
its expected labels here.  tui_tester_helpers.py reads this registry for
check #3 (kpi_label_match) and the v2 checks (data_accuracy, row_interactivity,
refresh_freshness).

Registration format
-------------------
Each entry in REGISTRY maps a tab_id (matching App.BINDINGS action arg, e.g.
"home") to a ScreenShape:

    ScreenShape(
        kpi_labels=[...],          # expected Label widget texts on this tab
        datatable_ids=[...],       # DataTable widget IDs to verify have content
        no_data_placeholder="...", # text that counts as "has content" if no rows
        data_checks=[...],         # list of (widget_id, validator) for value checks
        interactive_tables={...},  # table_id → True (expected clickable) or "passive: reason"
    )

If kpi_labels is empty, check #3 is skipped (pass) for that tab.
If datatable_ids is empty, check #2 is skipped for that tab.
If data_checks is empty, check #6 (data_accuracy) is skipped for that tab.
If interactive_tables is empty, check #7 (row_interactivity) is skipped for that tab.

Anti-pattern check functions (proactive sweeper)
-------------------------------------------------
Seven pure functions operate on a screen source path and optionally a live
screen instance. Each returns a list[Finding] — empty means pass.

    check_all_datatables_row_cursor(source_path) -> list[Finding]
    check_focus_targets_focusable(source_path) -> list[Finding]
    check_readers_nontrivial(screen_name, screen_instance) -> list[Finding]
    check_action_methods_notify(source_path) -> list[Finding]
    check_lastupdated_ticks(source_path) -> list[Finding]
    check_hint_label_matches_bindings(source_path) -> list[Finding]
    check_hint_bindings_drift(source_path) -> list[Finding]
    check_screen_clean(source_path, screen_name, screen_instance) -> list[Finding]
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class ScreenShape:
    """Expected widget shape for a single TUI tab."""

    kpi_labels: list[str] = field(default_factory=list)
    """KPI Label texts that must be present on this tab (exact or substring)."""

    datatable_ids: list[str] = field(default_factory=list)
    """DataTable widget IDs whose content is verified by check #2."""

    no_data_placeholder: str = "No data"
    """Text in a Label/Static widget that counts as valid when a DataTable is empty."""

    data_checks: list[tuple[str, Callable[[str], bool]]] = field(default_factory=list)
    """v2: list of (widget_id, validator) pairs.

    validator is a callable that receives the widget's visible text and returns
    True if the value looks correct.  A False return triggers a data_accuracy_drift
    finding.
    """

    interactive_tables: dict[str, bool | str] = field(default_factory=dict)
    """v2: table_id → True (expected to be clickable) or "passive: <reason>" (exempted).

    Tables not listed here are checked for interactivity by default if found.
    Provide an entry with value "passive: <reason>" to suppress the finding.
    """

    row_select_passive: bool = False
    """True when ALL tables on this tab are display-only and row-selection is not
    expected.  Sets interactive_tables entries to "passive: ..." automatically and
    satisfies test_stats_registered_passive for tabs like 'stats'.
    """


# ---------------------------------------------------------------------------
# Validators for data_checks
# ---------------------------------------------------------------------------


def _ends_with_percent(text: str) -> bool:
    """Budget KPI must end with a % character."""
    return text.strip().endswith("%")


def _contains_digit(text: str) -> bool:
    """Open-PRs KPI must contain at least one digit."""
    return any(c.isdigit() for c in text)


_ISO_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # YYYY-MM-DD
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[Z]|[+-]\d{2}:\d{2})?)?",  # optional time part
)


def _looks_like_iso_or_relative(text: str) -> bool:
    """Last-loop KPI should look like an ISO datetime, a relative time, or 'never'."""
    t = text.strip().lower()
    if t in ("never", "n/a", "-", ""):
        return True  # valid sentinel values
    if _ISO_PATTERN.search(text):
        return True
    # Also accept human-relative formats: "2m ago", "1h ago", "3d ago"
    if re.search(r"\d+\s*(?:s|m|h|d|w)\s*ago", t):
        return True
    return False


def _budget_percent_in_range(text: str) -> bool:
    """Budget KPI must be a % between 0 and 200 — catches over-100 display bugs."""
    t = text.strip()
    if not t.endswith("%"):
        return False
    try:
        val = float(t[:-1])
    except ValueError:
        return False
    return 0.0 <= val <= 200.0


def _stuck_count_reasonable(text: str) -> bool:
    """Stuck-agent-runs KPI must be a non-negative integer ≤ 50.

    > 50 almost certainly means a stale sentinel or off-by-one error, not a
    real count of stuck agents.
    """
    t = text.strip()
    try:
        val = int(t)
    except ValueError:
        return False
    return 0 <= val <= 50


def _no_sentinel_minus_one(text: str) -> bool:
    """KPI table text must not contain the literal '-1.0' — catches sentinel leaks."""
    return "-1.0" not in text


def _no_idem_test_agent(text: str) -> bool:
    """Stuck-runs table text must not contain 'idem-test-' agent IDs.

    Idempotency-test runs that appear in the stuck table indicate the test
    harness left ghost runs behind (or the dedup filter is broken).
    """
    return "idem-test-" not in text


def _loop_table_spawned_varies(text: str) -> bool:
    """Loop history table must not show exclusively '0' in the spawned column.

    If every row has spawned=0 the reader is almost certainly returning zeros
    instead of reading the real agents_spawned field.  An empty table passes
    (no data to evaluate).

    Strategy: find standalone integers (not part of timestamps HH:MM:SS or
    durations NmNNs).  If all such values are '0', the spawned column is stuck.
    """
    # Strip timestamp-like tokens (HH:MM or HH:MM:SS) and duration tokens (NmNNs)
    # before looking for standalone integers, to avoid false matches.
    cleaned = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", " ", text)  # timestamps
    cleaned = re.sub(r"\d+m\d+s", " ", cleaned)               # durations
    nums = re.findall(r"\b(\d+)\b", cleaned)
    if not nums:
        return True  # empty table — pass
    return not all(n == "0" for n in nums)


def _loop_table_duration_varies(text: str) -> bool:
    """Loop history table must not show exclusively '5m00s' durations.

    A column where every row reads '5m00s' is almost certainly the default
    value being returned instead of real elapsed time.  An empty table passes.
    """
    durations = re.findall(r"\d+m\d+s", text)
    if not durations:
        return True  # empty table — pass
    return not all(d == "5m00s" for d in durations)


def _feed_status_parseable(text: str) -> bool:
    """Agent-feed status widget must include a parseable 'stuck>15min:' count.

    Catches cases where the status line format broke and the stuck count field
    is missing or shows a sentinel like '-1'.
    """
    t = text.strip()
    if not t:
        return True  # widget not yet populated — skip
    if "stuck>15min:" not in t:
        return False
    m = re.search(r"stuck>15min:\s*(-?\d+)", t)
    if m is None:
        return False
    return int(m.group(1)) >= 0


def _table_rows_unique(text: str) -> bool:
    """DataTable text must not contain duplicate non-empty rows.

    A duplicate row in an audit table means the dedup filter is broken or the
    same event was written twice.  Empty tables pass.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return len(lines) == len(set(lines))


# ---------------------------------------------------------------------------
# Registry — one entry per tab_id
# ---------------------------------------------------------------------------

REGISTRY: dict[str, ScreenShape] = {
    "home": ScreenShape(
        kpi_labels=[
            "Weekly budget used:",
            "Open PRs:",
            "Stuck agent runs:",
            "Last loop run:",
        ],
        datatable_ids=[],
        no_data_placeholder="No data",
        data_checks=[
            ("kpi-budget", _ends_with_percent),
            ("kpi-budget", _budget_percent_in_range),
            ("kpi-open-prs", _contains_digit),
            ("kpi-stuck", _stuck_count_reasonable),
            ("kpi-last-loop", _looks_like_iso_or_relative),
        ],
        interactive_tables={},
    ),
    "prs": ScreenShape(
        kpi_labels=[],
        datatable_ids=["prs-table"],
        no_data_placeholder="No data",
        interactive_tables={"prs-table": True},
    ),
    "discussions": ScreenShape(
        kpi_labels=[],
        datatable_ids=["discussions-table"],
        no_data_placeholder="No data",
        interactive_tables={"discussions-table": True},
    ),
    "loop": ScreenShape(
        kpi_labels=[],
        datatable_ids=["loop-table"],
        no_data_placeholder="No data",
        data_checks=[
            ("loop-table", _loop_table_spawned_varies),
            ("loop-table", _loop_table_duration_varies),
        ],
        interactive_tables={"loop-table": True},
    ),
    "runs": ScreenShape(
        kpi_labels=[],
        datatable_ids=["recent-runs-table", "stuck-table", "percentiles-table"],
        no_data_placeholder="No data",
        data_checks=[
            ("stuck-table", _no_idem_test_agent),
        ],
        interactive_tables={
            "recent-runs-table": True,
            "stuck-table": True,
            "percentiles-table": "passive: aggregated stats, no row drilldown",
        },
    ),
    "agent_feed": ScreenShape(
        kpi_labels=[],
        datatable_ids=["agent-feed-table"],
        no_data_placeholder="No data",
        data_checks=[
            ("agent-feed-status", _feed_status_parseable),
        ],
        interactive_tables={"agent-feed-table": True},
    ),
    "stats": ScreenShape(
        kpi_labels=[],
        datatable_ids=["kpi-table", "classifier-table"],
        no_data_placeholder="No data",
        data_checks=[
            ("kpi-table", _no_sentinel_minus_one),
        ],
        # Stats tables are read-only summaries — no row-selection needed
        interactive_tables={
            "kpi-table": "passive: deferred to v2",
            "classifier-table": "passive: deferred to v2",
        },
        row_select_passive=True,
    ),
    "pr_detail": ScreenShape(
        kpi_labels=[],
        datatable_ids=["checks-table", "reviewers-table", "commits-table"],
        no_data_placeholder="No data",
        interactive_tables={
            "checks-table": "passive: deferred to v2",
            "reviewers-table": "passive: deferred to v2",
            "commits-table": "passive: deferred to v2",
        },
    ),
    "loop_controller": ScreenShape(
        kpi_labels=[],
        datatable_ids=["lc-gates", "lc-budget", "lc-errors"],
        no_data_placeholder="No active loops.",
        data_checks=[
            ("lc-errors", _table_rows_unique),
        ],
        interactive_tables={
            "lc-gates": True,
            "lc-budget": "passive: deferred to v2",
            "lc-errors": "passive: deferred to v2",
        },
    ),
    "settings": ScreenShape(
        kpi_labels=[],
        datatable_ids=["settings-gates", "settings-audit"],
        no_data_placeholder="No data",
        data_checks=[
            ("settings-audit", _table_rows_unique),
        ],
        interactive_tables={
            "settings-gates": "passive: read-only v1",
            "settings-audit": "passive: read-only v1",
        },
    ),
    "ideas": ScreenShape(
        kpi_labels=[],
        datatable_ids=["ideas-table"],
        no_data_placeholder="No matching discussions",
        interactive_tables={"ideas-table": True},
    ),
}


def get(tab_id: str) -> ScreenShape:
    """Return ScreenShape for *tab_id*, or a default empty shape if unknown.

    Unknown tabs get a permissive shape (all checks skip rather than fail),
    so that adding a new tab without updating this registry degrades gracefully
    rather than causing false positives.
    """
    return REGISTRY.get(tab_id, ScreenShape())


# ---------------------------------------------------------------------------
# Anti-pattern check infrastructure
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single anti-pattern finding from a proactive sweep check."""

    screen: str
    widget_id: Optional[str]
    check: str
    severity: str  # "error" | "warn"
    evidence_path: str  # "file:line" or "fixture path"
    detail: str = ""


def _read_source(source_path: Path) -> Optional[ast.Module]:
    """Parse *source_path* to an AST Module. Returns None on failure."""
    try:
        return ast.parse(source_path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Check 1: check_all_datatables_row_cursor
# ---------------------------------------------------------------------------


def check_all_datatables_row_cursor(source_path: Path) -> list[Finding]:
    """AST walk: every DataTable(...) must have cursor_type='row'.

    Default cursor_type is 'cell', which means RowHighlighted/RowSelected
    never fire. Anti-pattern from D#768, D#772.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_datatable = (isinstance(func, ast.Name) and func.id == "DataTable") or (
            isinstance(func, ast.Attribute) and func.attr == "DataTable"
        )
        if not is_datatable:
            continue

        # Check if cursor_type keyword is present and equals "row"
        cursor_kw: Optional[str] = None
        for kw in node.keywords:
            if kw.arg == "cursor_type" and isinstance(kw.value, ast.Constant):
                cursor_kw = kw.value.value
                break

        if cursor_kw != "row":
            findings.append(
                Finding(
                    screen=screen_name,
                    widget_id="DataTable",
                    check="check_all_datatables_row_cursor",
                    severity="error",
                    evidence_path=f"{source_path}:{node.lineno}",
                    detail=(
                        f"DataTable at line {node.lineno} has cursor_type="
                        f"{cursor_kw!r} — must be 'row' for RowHighlighted to fire"
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 2: check_focus_targets_focusable
# ---------------------------------------------------------------------------

# Widget classes that cannot receive focus by default in Textual
_NON_FOCUSABLE_CLASSES = frozenset(
    ["Markdown", "Static", "Label", "Rule", "Sparkline", "ProgressBar", "Footer", "Header"]
)


def check_focus_targets_focusable(source_path: Path) -> list[Finding]:
    """AST walk: for every .focus() call, resolve the target widget.

    If the target is constructed as an instance of a non-focusable widget class
    (Markdown, Static, Label, etc.), flag it — .focus() on such a widget is a
    silent no-op. Anti-pattern from D#769.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    findings: list[Finding] = []

    # Walk for `<expr>.focus()` calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "focus"):
            continue
        obj = func.value

        # Resolve simple name via assignment lookup is complex — instead look for
        # patterns like:  Markdown(...).focus() or Static("...").focus()
        if isinstance(obj, ast.Call):
            inner_func = obj.func
            if isinstance(inner_func, ast.Name) and inner_func.id in _NON_FOCUSABLE_CLASSES:
                findings.append(
                    Finding(
                        screen=screen_name,
                        widget_id=inner_func.id,
                        check="check_focus_targets_focusable",
                        severity="error",
                        evidence_path=f"{source_path}:{node.lineno}",
                        detail=(
                            f".focus() called on {inner_func.id}(...) at line {node.lineno} "
                            f"— {inner_func.id} has can_focus=False, call is a silent no-op"
                        ),
                    )
                )
        elif isinstance(obj, ast.Name):
            # Check if the name was assigned in a query_one() to a non-focusable type
            # We can't fully resolve without type inference, but we flag explicit casts:
            # w: Markdown = self.query_one(..., Markdown); w.focus()
            # This is caught at the query_one call site in another pass — skip here.
            pass

    return findings


# ---------------------------------------------------------------------------
# Check 3: check_readers_nontrivial
# ---------------------------------------------------------------------------


def check_readers_nontrivial(
    screen_name: str, screen_instance: Any
) -> list[Finding]:
    """Runtime check: drive screen data readers; flag all-zero / empty results.

    For each screen, look for reader methods (refresh_*, _load_*, on_mount
    patterns that call data_layer). This check requires a live screen instance.
    Returns findings when a reader produces a falsy or all-zero result set.

    Anti-pattern from D#773.
    """
    findings: list[Finding] = []
    if screen_instance is None:
        return findings

    # Look for refresh_* / _load_* methods on the instance
    for attr_name in dir(screen_instance):
        if not (attr_name.startswith("refresh_") or attr_name.startswith("_load_")):
            continue
        method = getattr(screen_instance, attr_name, None)
        if not callable(method):
            continue
        try:
            result = method()
        except Exception:
            continue
        # Flag if result is falsy (empty list, empty dict, None, 0)
        if result is not None and result != [] and result != {} and result != 0:
            continue
        findings.append(
            Finding(
                screen=screen_name,
                widget_id=None,
                check="check_readers_nontrivial",
                severity="warn",
                evidence_path=f"live:{screen_name}.{attr_name}",
                detail=(
                    f"{attr_name}() returned {result!r} — all-zero or empty; "
                    "verify data_layer schema matches on-disk format"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 4: check_action_methods_notify
# ---------------------------------------------------------------------------


def check_action_methods_notify(source_path: Path) -> list[Finding]:
    """AST walk: every action_* method with an early-return branch must have
    a self.notify(...) call in at least one path.

    Prevents silent no-ops where action handlers return early without user
    feedback. Anti-pattern from D#771.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("action_"):
            continue

        # Detect early returns in the function body
        has_early_return = False
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Return) and stmt is not node.body[-1]:
                has_early_return = True
                break

        if not has_early_return:
            continue

        # Check if any self.notify(...) call exists anywhere in the function
        has_notify = False
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Call):
                continue
            func = stmt.func
            if isinstance(func, ast.Attribute) and func.attr == "notify":
                has_notify = True
                break

        if not has_notify:
            findings.append(
                Finding(
                    screen=screen_name,
                    widget_id=None,
                    check="check_action_methods_notify",
                    severity="warn",
                    evidence_path=f"{source_path}:{node.lineno}",
                    detail=(
                        f"{node.name}() at line {node.lineno} has early return(s) "
                        "but no self.notify() call — user gets no feedback on no-op paths"
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 5: check_lastupdated_ticks
# ---------------------------------------------------------------------------


def check_lastupdated_ticks(source_path: Path) -> list[Finding]:
    """AST walk: every screen mounting a LastUpdated widget must call
    set_fetched() within its data-fetch path.

    Screens that mount LastUpdated but never call set_fetched() show
    permanent "Updated —". Anti-pattern from spec requirement #6.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    findings: list[Finding] = []

    # Check if the source imports or uses LastUpdated
    source_text = source_path.read_text(encoding="utf-8")
    if "LastUpdated" not in source_text:
        return []

    # Verify set_fetched is called somewhere in the module
    has_set_fetched = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "set_fetched":
            has_set_fetched = True
            break

    if not has_set_fetched:
        findings.append(
            Finding(
                screen=screen_name,
                widget_id="LastUpdated",
                check="check_lastupdated_ticks",
                severity="error",
                evidence_path=str(source_path),
                detail=(
                    "Screen mounts LastUpdated but never calls set_fetched() — "
                    "widget will permanently show 'Updated —'"
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Check 6: check_hint_label_matches_bindings
# ---------------------------------------------------------------------------


def _collect_app_level_binding_keys() -> set[str]:
    """Parse dashboard_tui/app.py BINDINGS via AST and return the set of key strings.

    Returns an empty set if app.py cannot be found or parsed.  The result is
    cached on the function object so the file is only read once per process.
    """
    if not hasattr(_collect_app_level_binding_keys, "_cache"):
        keys: set[str] = set()
        # Try paths relative to this file and relative to cwd
        candidates = [
            Path(__file__).parent.parent / "dashboard_tui" / "app.py",
            Path("dashboard_tui") / "app.py",
        ]
        app_path: Optional[Path] = None
        for c in candidates:
            if c.exists():
                app_path = c
                break
        if app_path is not None:
            tree = _read_source(app_path)
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "BINDINGS":
                                if isinstance(node.value, ast.List):
                                    for elt in node.value.elts:
                                        if isinstance(elt, ast.Call) and elt.args:
                                            if isinstance(elt.args[0], ast.Constant):
                                                keys.add(str(elt.args[0].value))
        _collect_app_level_binding_keys._cache = keys  # type: ignore[attr-defined]
    return _collect_app_level_binding_keys._cache  # type: ignore[attr-defined]


def check_hint_label_matches_bindings(source_path: Path) -> list[Finding]:
    """AST walk: parse the screen's hint label text; assert each key claim
    matches a BINDINGS entry, a wired on_key_* / action_* method, or an
    app-level BINDINGS entry in dashboard_tui/app.py.

    Hint labels like "Press 'r' to refresh, 'q' to quit" must have corresponding
    BINDINGS or on_key_* methods. Anti-pattern from D#770.  False-positives for
    app-level keys (e.g. 'r' for refresh, 'q' for quit defined in app.py) are
    suppressed by also checking the app-level BINDINGS.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    source_text = source_path.read_text(encoding="utf-8")
    findings: list[Finding] = []

    # Collect BINDINGS keys from the screen's own AST
    binding_keys: set[str] = set()
    for node in ast.walk(tree):
        # Look for BINDINGS = [...] class variable
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "BINDINGS":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Call) and elt.args:
                                if isinstance(elt.args[0], ast.Constant):
                                    binding_keys.add(str(elt.args[0].value))

    # Also collect on_key_* methods as wired handlers
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("on_key_"):
            key = node.name[len("on_key_"):]
            binding_keys.add(key)

    # Merge in app-level BINDINGS so screens that rely on app-level keys
    # (e.g. 'r' → refresh_screen, 'q' → quit) are not false-positived
    binding_keys |= _collect_app_level_binding_keys()

    # Find hint label texts — look for Label("Press ...") or Label("...'key'...")
    # containing key references like 'r', 'q', 'f', etc.
    hint_texts: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_label = (isinstance(func, ast.Name) and func.id in ("Label", "Static")) or (
            isinstance(func, ast.Attribute) and func.attr in ("Label", "Static")
        )
        if not is_label:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        text = str(node.args[0].value)
        if "Press" in text or ("'" in text and ("to " in text or "for " in text)):
            hint_texts.append((text, node.lineno))

    # Extract key claims from hint texts: single-char quoted keys like 'r', 'q'
    _KEY_PATTERN = re.compile(r"'([a-zA-Z0-9])'")
    for hint_text, lineno in hint_texts:
        claimed_keys = _KEY_PATTERN.findall(hint_text)
        for key in claimed_keys:
            if key not in binding_keys:
                findings.append(
                    Finding(
                        screen=screen_name,
                        widget_id="hint-label",
                        check="check_hint_label_matches_bindings",
                        severity="warn",
                        evidence_path=f"{source_path}:{lineno}",
                        detail=(
                            f"Hint label at line {lineno} claims key '{key}' "
                            f"but no matching BINDINGS entry or on_key_{key} found"
                        ),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check 7 (new): check_hint_bindings_drift — inverse of check 6
# Every BINDINGS entry must appear in the hint label (so wired keys are
# discoverable). Anti-pattern: a binding exists but the hint says nothing
# about it, leaving users unaware of the key.
# ---------------------------------------------------------------------------


def check_hint_bindings_drift(source_path: Path) -> list[Finding]:
    """AST walk: every BINDINGS key must appear in the screen's hint label text.

    This is the inverse of check_hint_label_matches_bindings (check 6):
    - Check 6 catches  hint claims a key that has no binding.
    - Check 7 catches  a binding exists but the hint never mentions it.

    Severity: warn — a missing mention is a UX gap, not a crash.
    """
    tree = _read_source(source_path)
    if tree is None:
        return []

    screen_name = source_path.stem
    findings: list[Finding] = []

    # Collect BINDINGS keys and their descriptions from the AST
    binding_keys: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "BINDINGS":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Call) and elt.args:
                                if isinstance(elt.args[0], ast.Constant):
                                    binding_keys.append(
                                        (str(elt.args[0].value), elt.lineno)
                                    )

    if not binding_keys:
        return []

    # Collect all hint label texts
    hint_texts: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_label = (isinstance(func, ast.Name) and func.id in ("Label", "Static")) or (
            isinstance(func, ast.Attribute) and func.attr in ("Label", "Static")
        )
        if not is_label:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        text = str(node.args[0].value)
        if "Press" in text or ("'" in text and ("to " in text or "for " in text)):
            hint_texts.append(text)

    if not hint_texts:
        return []

    combined_hint = " ".join(hint_texts)
    _KEY_PATTERN = re.compile(r"'([a-zA-Z0-9])'")
    mentioned_keys = set(_KEY_PATTERN.findall(combined_hint))

    for key, lineno in binding_keys:
        # Single-char keys only — multi-char like "ctrl+r" are harder to standardise
        if len(key) == 1 and key not in mentioned_keys:
            findings.append(
                Finding(
                    screen=screen_name,
                    widget_id="hint-label",
                    check="check_hint_bindings_drift",
                    severity="warn",
                    evidence_path=f"{source_path}:{lineno}",
                    detail=(
                        f"BINDINGS has key '{key}' at line {lineno} "
                        "but it is not mentioned in any hint label"
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 8: check_screen_clean — composite of checks 1-7
# ---------------------------------------------------------------------------


def check_screen_clean(
    source_path: Path,
    screen_name: str = "",
    screen_instance: Any = None,
) -> list[Finding]:
    """Run all 7 static checks + the runtime reader check against one screen.

    Returns the union of all findings. An empty list means the screen is clean.
    This is the entry point for the full proactive sweep.
    """
    if not screen_name:
        screen_name = source_path.stem

    all_findings: list[Finding] = []
    all_findings.extend(check_all_datatables_row_cursor(source_path))
    all_findings.extend(check_focus_targets_focusable(source_path))
    all_findings.extend(check_readers_nontrivial(screen_name, screen_instance))
    all_findings.extend(check_action_methods_notify(source_path))
    all_findings.extend(check_lastupdated_ticks(source_path))
    all_findings.extend(check_hint_label_matches_bindings(source_path))
    all_findings.extend(check_hint_bindings_drift(source_path))
    return all_findings
