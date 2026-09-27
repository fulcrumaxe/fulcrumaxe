"""scripts/lib/pytest_baseline.py — record, diff, manifest and check pytest
full-suite baselines.

Four subcommands, one shared record schema (D#2403 PR 2 of 5 introduced
``record``/``diff``; D#1900 PR 3 adds ``manifest``/``check``, library and
tests only — no CI wiring lives here):

  record   junit-xml + context JSON  ->  one JSON record on stdout
  diff     a directory of records    ->  analysis.json on stdout
  manifest junit-xml                 ->  a named set of bad node ids on stdout
  check    a manifest + a junit-xml  ->  exit 0/non-zero, gate-shaped

Design point: capture richly, dedup late. A record's ``outcomes`` list keeps
failure/error/collection_error distinct exactly as pytest's junit-xml reports
them — the FAILED-vs-ERROR dedup question is answered at diff/manifest time,
under a named ``--dedup-convention``, so it is a parameter of the analysis
rather than a property baked into the capture.

Every record carries the four terms that make a number reproducible:
path scope (``path_scope.argv`` — the actual argv, not a label for it),
dedup convention at *capture* time (``dedup_convention`` — a fixed constant
naming the structural-vs-grepped capture method, held constant by this module;
this is distinct from the ``--dedup-convention`` diff/manifest flag, which
decides how outcome kinds are bucketed into "bad" at analysis time), load, and
checkout cleanliness. ``diff`` refuses to compare records whose path_scope or
dedup_convention disagree (never merges apples with oranges) — that is the
one thing this module hard-fails on rather than warns about.

``manifest`` and ``check`` extend that discipline to a *named set of node
ids*, never a count, per D#1900's consensus: a count-delta gate is silent
about which test moved and passes green when one fix trades for one
regression. ``check`` is node-id granular by construction — a manifest entry
that is not a fully-qualified node id (a bare module, a glob, a trailing
``*``) is a load-time error, never a warning, because a quarantined module
would hide a new failure inside it, which is the exact defect this baseline
tooling exists to end. A manifest that cannot be read at all (missing,
unparseable, or shaped wrong) is a hard, fail-closed refusal — but a manifest
that was never generated in the first place (the adopter/fresh-clone path) is
report-only, never blocked, since our own failures are not theirs to inherit.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Capture-time convention identifier. Fixed: this module always distinguishes
# failure/error/collection_error structurally via junit-xml, never by
# grepping terminal output. If that capture method ever changes, bump this
# constant — diff()/check() then correctly refuse to merge old-style records
# with new-style ones instead of silently treating them as comparable.
CAPTURE_DEDUP_CONVENTION = "junit-structural-v1"

# Default '+'-joined outcome kinds counted as "bad" when neither diff() nor
# manifest() is told otherwise. Never includes collection_error by default —
# a whole-module import break is a different failure shape than a per-test
# failure/error and callers that care about it opt in explicitly.
DEFAULT_DEDUP_CONVENTION = "failure+error"

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
    """Raised for a malformed record/manifest — callers should exit non-zero."""


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


# ── manifest / check (D#1900 PR 3) ──────────────────────────────────────────


def _generated_at_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generated_sha() -> str:
    """Best-effort sha for the tree the manifest was generated from. CI's own
    checkout sha (GITHUB_SHA) wins when present, since that is the sha the
    gate actually guards; otherwise fall back to a local `git rev-parse
    HEAD`. Never raises — an unresolvable sha is 'unknown', not a crash,
    since the sha is provenance for humans, not a value anything compares
    against."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "unknown"


def _generated_on() -> str:
    return f"{platform.system()} {platform.release()} / Python {platform.python_version()}"


def _manifest_age(generated_at: Optional[str]) -> str:
    """Seconds since `generated_at`, as `<N>s` — or 'unknown' when
    `generated_at` is missing or unparseable. A relative age, not the
    absolute timestamp, is what makes staleness visible at a glance on
    every PR run (D#1900 answer to Lena's question 1)."""
    if not generated_at:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    return f"{max(0, int(delta_seconds))}s"


def junit_id_to_pytest_nodeid(node_id: str) -> str:
    """Translate this module's capture-format node id (junit's dotted
    classname, `::`, the test name) into a pytest-runnable node id.

    Examples:
      'backend.tests.test_blackboard::test_x'
        -> 'backend/tests/test_blackboard.py::test_x'
      'tests.test_foo.TestBar::test_x'
        -> 'tests/test_foo.py::TestBar::test_x'
      'backend.tests.test_foo::test_x[param-id]'
        -> 'backend/tests/test_foo.py::test_x[param-id]' (unchanged past '::')

    Class-name segments are found by a leading uppercase letter — pytest
    modules are snake_case, test classes are PascalCase (PEP8) — walking the
    dotted classname from the right until a lowercase-leading (module)
    segment is hit. This is the single most likely place for a
    plausible-but-wrong string to ship (D#1900 item 4), which is why every
    caller of this function is expected to execute the result under
    `--collect-only -q`, never merely compare it as a string."""
    if "::" not in node_id:
        raise ValueError(f"not a fully-qualified node id (no '::'): {node_id!r}")
    classname, name = node_id.split("::", 1)
    if not classname or not name:
        raise ValueError(f"not a fully-qualified node id: {node_id!r}")
    parts = classname.split(".")
    class_parts: List[str] = []
    while parts and parts[-1][:1].isupper():
        class_parts.insert(0, parts.pop())
    if not parts:
        raise ValueError(f"could not locate a module path in: {node_id!r}")
    module_path = "/".join(parts) + ".py"
    if class_parts:
        return f"{module_path}::{'::'.join(class_parts)}::{name}"
    return f"{module_path}::{name}"


def cmd_manifest(args: argparse.Namespace) -> int:
    junit_path = Path(args.junit_xml)
    outcomes = parse_junit_outcomes(junit_path)
    outcome_kinds = set(args.dedup_convention.split("+"))
    node_ids = sorted({o["node_id"] for o in outcomes if o["outcome"] in outcome_kinds})

    manifest = {
        "node_ids": node_ids,
        "dedup_convention": CAPTURE_DEDUP_CONVENTION,
        "outcome_kinds": args.dedup_convention,
        "generated_at": _generated_at_now(),
        "generated_sha": _generated_sha(),
        "generated_on": _generated_on(),
    }
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _validate_manifest_node_id(entry: Any) -> None:
    """A manifest entry must be a fully-qualified node id — never a bare
    module, a glob, or a trailing wildcard. Ruling module-level quarantine
    out is D#1900's whole point: a new failure inside a quarantined module
    would go invisible, reintroducing the exact defect one level up."""
    if not isinstance(entry, str) or not entry:
        raise BaselineError(f"manifest entry is not a fully-qualified node id: {entry!r}")
    if "*" in entry:
        raise BaselineError(f"manifest entry is not a fully-qualified node id (glob): {entry!r}")
    if "::" not in entry:
        raise BaselineError(
            f"manifest entry is not a fully-qualified node id (bare module, no '::'): {entry!r}"
        )
    prefix, _, suffix = entry.rpartition("::")
    if not prefix or not suffix:
        raise BaselineError(f"manifest entry is not a fully-qualified node id: {entry!r}")


def _load_manifest(path: Path) -> Dict[str, Any]:
    """Read and minimally validate a manifest file. Raises BaselineError for
    every way a *present* manifest can be unusable — unreadable, empty,
    unparseable JSON, or missing `node_ids` — because an unreadable manifest
    must fail closed, not silently degrade to report-only. Only a genuinely
    *absent* manifest (checked by the caller before this is called) is
    report-only."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise BaselineError(f"could not read manifest {path}: {exc}") from exc
    if not text.strip():
        raise BaselineError(f"manifest is empty: {path}")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"manifest is not valid JSON: {path} ({exc})") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("node_ids"), list):
        raise BaselineError(f"manifest missing required 'node_ids' list: {path}")
    return manifest


def cmd_check(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest) if args.manifest else None

    if manifest_path is None or not manifest_path.is_file():
        where = args.manifest if args.manifest else "(none given)"
        print(
            f"[pytest_baseline check] report-only: no manifest at {where} — "
            "nothing to compare against, this run is advisory only"
        )
        return 0

    try:
        manifest = _load_manifest(manifest_path)
    except BaselineError as exc:
        sys.stderr.write(f"[pytest_baseline] {exc}\n")
        return 1

    try:
        for entry in manifest["node_ids"]:
            _validate_manifest_node_id(entry)
    except BaselineError as exc:
        sys.stderr.write(f"[pytest_baseline] {exc}\n")
        return 1

    manifest_dedup = manifest.get("dedup_convention")
    if manifest_dedup != CAPTURE_DEDUP_CONVENTION:
        sys.stderr.write(
            "[pytest_baseline] fixed term 'dedup_convention' differs between manifest and "
            f"this tool's junit capture — refusing to compare (manifest={manifest_dedup!r} "
            f"vs capture={CAPTURE_DEDUP_CONVENTION!r})\n"
        )
        return 1

    outcome_kinds_str = manifest.get("outcome_kinds") or DEFAULT_DEDUP_CONVENTION
    outcome_kinds = set(outcome_kinds_str.split("+"))

    manifest_node_ids = set(manifest["node_ids"])
    junit_path = Path(args.junit_xml)
    outcomes = parse_junit_outcomes(junit_path)
    bad_ids = {o["node_id"] for o in outcomes if o["outcome"] in outcome_kinds}

    size = len(manifest_node_ids)
    sha = manifest.get("generated_sha", "unknown")
    age = _manifest_age(manifest.get("generated_at"))
    print(f"[pytest_baseline check] manifest size={size} sha={sha} age={age}")

    new_bad = sorted(bad_ids - manifest_node_ids)
    if new_bad:
        for node_id in new_bad:
            try:
                repro = f"python3 -m pytest {junit_id_to_pytest_nodeid(node_id)}"
            except ValueError:
                repro = "(could not translate to a pytest-runnable node id)"
            print(f"NEW FAILURE not in manifest: {node_id}")
            print(f"  reproduce: {repro}")
        return 1

    quarantined_passing = sorted(manifest_node_ids - bad_ids)
    if quarantined_passing:
        print(f"{len(quarantined_passing)} quarantined tests now pass — regenerate")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytest_baseline.py",
        description="Record, diff, manifest and check pytest full-suite baselines.",
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
        default=DEFAULT_DEDUP_CONVENTION,
        help=f"'+'-joined outcome kinds counted as bad (default: {DEFAULT_DEDUP_CONVENTION})",
    )
    p_diff.set_defaults(func=cmd_diff)

    p_manifest = sub.add_parser("manifest", help="junit-xml -> a named manifest of bad node ids")
    p_manifest.add_argument("--junit-xml", required=True, help="path to junit-xml")
    p_manifest.add_argument(
        "--dedup-convention",
        default=DEFAULT_DEDUP_CONVENTION,
        help=f"'+'-joined outcome kinds counted as bad (default: {DEFAULT_DEDUP_CONVENTION})",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    p_check = sub.add_parser("check", help="compare a junit-xml run against a manifest")
    p_check.add_argument(
        "--manifest",
        default=None,
        help="path to a manifest JSON (missing/omitted -> report-only, exit 0)",
    )
    p_check.add_argument("--junit-xml", required=True, help="path to junit-xml")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
