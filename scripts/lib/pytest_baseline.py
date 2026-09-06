"""scripts/lib/pytest_baseline.py — record and diff pytest full-suite baselines.

Two subcommands, one shared record schema (D#2403 PR 2 of 5, measurement only):

  record   junit-xml + context JSON  ->  one JSON record on stdout
  diff     a directory of records    ->  analysis.json on stdout

Design point: capture richly, dedup late. A record's ``outcomes`` list keeps
failure/error/collection_error distinct exactly as pytest's junit-xml reports
them — the FAILED-vs-ERROR dedup question is answered at diff time, under a
named ``--dedup-convention``, so it is a parameter of the analysis rather than
a property baked into the capture.

Every record carries the four terms that make a number reproducible:
path scope (``path_scope.argv`` — the actual argv, not a label for it),
dedup convention at *capture* time (``dedup_convention`` — a fixed constant
naming the structural-vs-grepped capture method, held constant by this module;
this is distinct from the ``--dedup-convention`` diff flag, which decides how
outcome kinds are bucketed into "bad" at analysis time), load, and checkout
cleanliness. ``diff`` refuses to compare records whose path_scope or
dedup_convention disagree (never merges apples with oranges) — that is the
one thing this module hard-fails on rather than warns about.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

# Capture-time convention identifier. Fixed: this module always distinguishes
# failure/error/collection_error structurally via junit-xml, never by
# grepping terminal output. If that capture method ever changes, bump this
# constant — diff() then correctly refuses to merge old-style records with
# new-style ones instead of silently treating them as comparable.
CAPTURE_DEDUP_CONVENTION = "junit-structural-v1"

# Required top-level fields a context blob must supply. `record` computes
# `dedup_convention` and `outcomes` itself — everything else must come from
# the caller (the measurement harness), and a record missing any one of these
# is a hard error, not a defaulted field.
REQUIRED_CONTEXT_FIELDS = (
    "host",
    "path_scope",
    "checkout",
    "load",
    "other_pytest_running",
    "state_dir",
    "duration_seconds",
    "complete",
)

# Fixed terms diff() requires to agree across every record it compares.
# Anything else (load, checkout, duration, host) is expected to vary between
# runs — that variation is the whole point of the measurement.
FIXED_TERM_PATHS = (
    ("path_scope", "argv"),
    ("dedup_convention", None),
)


class BaselineError(Exception):
    """Raised for a malformed record/context — callers should exit non-zero."""


def _classify_outcome(elem: ET.Element) -> Optional[str]:
    """Return 'failure' / 'error' / 'collection_error' for one <testcase>, or
    None if it neither failed nor errored (passed or skipped — not a 'bad'
    outcome this measurement tracks)."""
    failure = elem.find("failure")
    if failure is not None:
        return "failure"
    error = elem.find("error")
    if error is not None:
        message = (error.get("message") or "").lower()
        if "collect" in message:
            return "collection_error"
        return "error"
    return None


def parse_junit_outcomes(junit_xml_path: Optional[Path]) -> List[Dict[str, str]]:
    """Parse a junit-xml file into a list of {node_id, outcome}. A missing or
    unparseable file yields an empty list rather than raising — that is the
    correct behaviour for a run killed mid-flight by the outer timeout, which
    is expected to leave no usable junit-xml and must still produce a record
    (with complete=false, supplied by the caller's context)."""
    if junit_xml_path is None or not junit_xml_path.is_file():
        return []
    try:
        tree = ET.parse(junit_xml_path)
    except ET.ParseError:
        return []
    outcomes = []
    for testcase in tree.getroot().iter("testcase"):
        outcome = _classify_outcome(testcase)
        if outcome is None:
            continue
        classname = testcase.get("classname") or ""
        name = testcase.get("name") or ""
        node_id = f"{classname}::{name}" if classname else name
        outcomes.append({"node_id": node_id, "outcome": outcome})
    return outcomes


def build_record(context: Dict[str, Any], junit_xml_path: Optional[Path]) -> Dict[str, Any]:
    missing = [f for f in REQUIRED_CONTEXT_FIELDS if f not in context]
    if missing:
        raise BaselineError(f"context missing required field(s): {', '.join(missing)}")

    record = dict(context)
    record["dedup_convention"] = CAPTURE_DEDUP_CONVENTION
    record["outcomes"] = parse_junit_outcomes(junit_xml_path)
    return record


def cmd_record(args: argparse.Namespace) -> int:
    context_path = Path(args.context)
    try:
        context = json.loads(context_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"[pytest_baseline] could not read context {context_path}: {exc}\n")
        return 1

    junit_path = Path(args.junit_xml) if args.junit_xml else None
    try:
        record = build_record(context, junit_path)
    except BaselineError as exc:
        sys.stderr.write(f"[pytest_baseline] {exc}\n")
        return 1

    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def _fixed_term(record: Dict[str, Any], path: tuple) -> Any:
    key, subkey = path
    value = record.get(key)
    if subkey is None:
        return value
    if isinstance(value, dict):
        return value.get(subkey)
    return None


def _check_fixed_terms(records: List[Dict[str, Any]], sources: List[str]) -> None:
    """Raise BaselineError naming the differing field if any two records
    disagree on a fixed term. Never merges records that were never
    comparable."""
    if not records:
        return
    baseline = records[0]
    for path in FIXED_TERM_PATHS:
        key, subkey = path
        label = key if subkey is None else f"{key}.{subkey}"
        expected = _fixed_term(baseline, path)
        for record, src in zip(records, sources):
            actual = _fixed_term(record, path)
            if actual != expected:
                raise BaselineError(
                    f"fixed term '{label}' differs across records — refusing to merge "
                    f"({sources[0]!r}={expected!r} vs {src!r}={actual!r})"
                )


def _bad_ids(record: Dict[str, Any], outcome_kinds: set) -> set:
    return {
        o["node_id"]
        for o in record.get("outcomes", [])
        if o.get("outcome") in outcome_kinds
    }


def _duration_correlation(records: List[Dict[str, Any]], node_ids: List[str]) -> Dict[str, Any]:
    correlation: Dict[str, Any] = {}
    for node_id in node_ids:
        failed_in = []
        passed_in = []
        for record in records:
            bad = {o["node_id"] for o in record.get("outcomes", [])}
            entry = {
                "arm": (record.get("load") or {}).get("arm"),
                "duration_seconds": record.get("duration_seconds"),
            }
            if node_id in bad:
                failed_in.append(entry)
            else:
                passed_in.append(entry)
        correlation[node_id] = {"failed_in": failed_in, "passed_in": passed_in}
    return correlation

# Named once so analysis.json documents the gap instead of leaving it in
# prose (Spec decision 3): CPU-only contention isolates duration but does not
# reproduce port/filesystem collisions between concurrently-running suites.
CONTENTION_LIMITATION = (
    "Contention is CPU-only (nproc/2 busy-loop spinners). It does not reproduce "
    "port or filesystem collisions between concurrently-running pytest suites — "
    "that is a different experiment, out of scope for this measurement."
)


def build_analysis(records: List[Dict[str, Any]], dedup_convention: str) -> Dict[str, Any]:
    outcome_kinds = set(dedup_convention.split("+"))

    idle = [r for r in records if (r.get("load") or {}).get("arm") == "idle" and r.get("complete")]
    contended = [r for r in records if (r.get("load") or {}).get("arm") == "contended" and r.get("complete")]

    def arm_stats(arm_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        sets = [_bad_ids(r, outcome_kinds) for r in arm_records]
        if sets:
            intersection = set.intersection(*sets)
            union = set.union(*sets)
        else:
            intersection = set()
            union = set()
        return {
            "intersection": sorted(intersection),
            "union": sorted(union),
            "instability": sorted(union - intersection),
            "run_count": len(arm_records),
        }

    idle_stats = arm_stats(idle)
    contended_stats = arm_stats(contended)

    idle_intersection = set(idle_stats["intersection"])
    contended_intersection = set(contended_stats["intersection"])
    symmetric_difference = idle_intersection ^ contended_intersection
    contended_only = contended_intersection - idle_intersection
    idle_only = idle_intersection - contended_intersection

    all_complete = idle + contended
    duration_correlation = _duration_correlation(all_complete, sorted(symmetric_difference))

    return {
        "outcome_kinds": dedup_convention,
        "idle": idle_stats,
        "contended": contended_stats,
        "load_sensitive": {
            "symmetric_difference": sorted(symmetric_difference),
            "contended_only": sorted(contended_only),
            "idle_only": sorted(idle_only),
        },
        "sufficient": len(idle) >= 3 and len(contended) >= 3,
        "contention_limitation": CONTENTION_LIMITATION,
        "duration_correlation": duration_correlation,
    }


def cmd_diff(args: argparse.Namespace) -> int:
    records_dir = Path(args.records)
    if not records_dir.is_dir():
        sys.stderr.write(f"[pytest_baseline] not a directory: {records_dir}\n")
        return 1

    paths = sorted(records_dir.glob("*.json"))
    records: List[Dict[str, Any]] = []
    sources: List[str] = []
    for p in paths:
        try:
            records.append(json.loads(p.read_text()))
            sources.append(str(p))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[pytest_baseline] could not read record {p}: {exc}\n")
            return 1

    if not records:
        sys.stderr.write(f"[pytest_baseline] no *.json records found in {records_dir}\n")
        return 1

    try:
        _check_fixed_terms(records, sources)
    except BaselineError as exc:
        sys.stderr.write(f"[pytest_baseline] {exc}\n")
        return 1

    analysis = build_analysis(records, args.dedup_convention)
    print(json.dumps(analysis, indent=2, sort_keys=True))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytest_baseline.py",
        description="Record and diff pytest full-suite baselines (D#2403 PR 2 of 5).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="junit-xml + context -> one JSON record")
    p_record.add_argument("--junit-xml", default=None, help="path to junit-xml (missing/unparseable -> empty outcomes)")
    p_record.add_argument("--context", required=True, help="path to context JSON")
    p_record.set_defaults(func=cmd_record)

    p_diff = sub.add_parser("diff", help="a directory of records -> analysis.json")
    p_diff.add_argument("--records", required=True, help="directory of *.json records")
    p_diff.add_argument(
        "--dedup-convention",
        default="failure+error",
        help="'+'-joined outcome kinds counted as bad (default: failure+error)",
    )
    p_diff.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
