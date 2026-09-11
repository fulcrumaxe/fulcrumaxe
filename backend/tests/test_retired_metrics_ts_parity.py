"""RETIRED_METRICS cross-language parity (D#2539 fix round).

backend/stats/metric_order.py's RETIRED_METRICS and dashboard/src/pages/
stats/retiredMetrics.ts's RETIRED_METRICS are two independent registries
that must stay in lockstep — each carries only a prose "PARITY NOTE"
pointing at the other, and nothing previously failed if one was updated
without the other.

This test reads the TypeScript source as text and extracts its object
literal with a small brace-counting parser (no TS toolchain dependency),
then compares it against the Python registry:

  - key sets must match exactly (a metric retired, or un-retired, on one
    side must be mirrored on the other)
  - for any key present on both sides, the retirement metadata that
    matters operationally (which PR retired it, and when) must agree —
    the same metric retired in both places under different PR numbers is
    the same divergence wearing a hat

Human-authored prose (the `reason` field) is intentionally not compared
verbatim: the two sides phrase it differently (Python: one parenthesized
string; TS: a concatenated multi-line literal), and byte-for-byte prose
equality is not the claim this test is making.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.stats.metric_order import RETIRED_METRICS as PY_RETIRED_METRICS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TS_PATH = _REPO_ROOT / "dashboard" / "src" / "pages" / "stats" / "retiredMetrics.ts"

# TS field name -> Python field name, for the metadata that must agree.
_METADATA_FIELDS = {
    "retiredByPr": "retired_by_pr",
    "retiredDate": "retired_date",
}


def _extract_object_literal(source: str, const_name: str) -> str:
    """Return the `{ ... }` body (braces included) of
    `export const <const_name>: ... = { ... }`, located by brace counting
    so nested braces (each entry is itself an object) don't truncate it.
    """
    m = re.search(rf"export const {re.escape(const_name)}\b[^=]*=\s*{{", source)
    if not m:
        raise AssertionError(
            f"could not find `export const {const_name} = {{` in {_TS_PATH}"
        )
    start = m.end() - 1  # index of the opening brace
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(
        f"unbalanced braces scanning `{const_name}` object literal in {_TS_PATH}"
    )


_ENTRY_KEY_RE = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*{")
_FIELD_RE = re.compile(r"(retiredByPr|retiredDate)\s*:\s*'([^']*)'")


def _parse_ts_registry(object_literal: str) -> dict[str, dict[str, str]]:
    """Parse `{ key1: { retiredByPr: '...', ... }, key2: {...} }` into a
    plain dict of dicts, by brace-counting each entry's nested object."""
    inner = object_literal[1:-1]  # strip outer braces
    entries: dict[str, dict[str, str]] = {}
    pos = 0
    n = len(inner)
    while pos < n:
        m = _ENTRY_KEY_RE.search(inner, pos)
        if not m:
            break
        key = m.group(1)
        brace_start = m.end() - 1
        depth = 0
        j = brace_start
        while j < n:
            if inner[j] == "{":
                depth += 1
            elif inner[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        entry_body = inner[brace_start : j + 1]
        entries[key] = dict(_FIELD_RE.findall(entry_body))
        pos = j + 1
    return entries


def _load_ts_registry() -> dict[str, dict[str, str]]:
    source = _TS_PATH.read_text()
    literal = _extract_object_literal(source, "RETIRED_METRICS")
    return _parse_ts_registry(literal)


class TestRetiredMetricsTsParity:
    def test_key_sets_match(self):
        ts_registry = _load_ts_registry()
        py_keys = set(PY_RETIRED_METRICS)
        ts_keys = set(ts_registry)
        assert ts_keys == py_keys, (
            "RETIRED_METRICS key sets diverge between "
            "backend/stats/metric_order.py "
            f"({sorted(py_keys)}) and "
            "dashboard/src/pages/stats/retiredMetrics.ts "
            f"({sorted(ts_keys)}) — a metric retired (or un-retired) on "
            "one side must be mirrored on the other."
        )

    def test_retirement_metadata_matches_for_shared_keys(self):
        ts_registry = _load_ts_registry()
        shared = set(PY_RETIRED_METRICS) & set(ts_registry)
        assert shared, "expected at least one metric registered on both sides"

        mismatches = []
        for name in sorted(shared):
            py_record = PY_RETIRED_METRICS[name]
            ts_record = ts_registry[name]
            for ts_field, py_field in _METADATA_FIELDS.items():
                py_value = py_record.get(py_field)
                ts_value = ts_record.get(ts_field)
                if py_value != ts_value:
                    mismatches.append(
                        f"{name}.{py_field}: python={py_value!r} "
                        f"ts.{ts_field}={ts_value!r}"
                    )
        assert not mismatches, (
            "retirement metadata diverges between the two registries "
            "(same metric, different retiring PR and/or date is the same "
            "defect as a key-set mismatch):\n" + "\n".join(mismatches)
        )
