#!/usr/bin/env python3
"""merge-gate-parity-guard.py — the two merge paths may differ, but only on
record (D#2332).

The defect
----------
There are two ways a PR reaches ``main``: the loop auto-merge path
(``scripts/loop-phased-step5.sh``, merging phase) and the manual path
(``scripts/merge-and-hook.sh``). They enforce different sets of gates, and
nothing anywhere compared them.

That was found by accident. ``browser-test-passed`` gated the loop path and
not the manual one, so a five-file dashboard PR merged manually carrying one
label and no browser test — the identical PR would have been blocked at the
loop's merging phase. Fixing that one gate fixes one gate. It does not stop
the next one from drifting apart the same way, and the next one will also be
found by accident or not at all.

What this guard is for
----------------------
Not "list the known differences". A hand-maintained list of differences is the
same defect one level up: it agrees with the documentation, it is very hard to
disprove, and it goes stale in the direction of claiming more parity than
exists. **A parity guard that certifies parity which does not exist is worse
than no guard.**

So the subject set is *derived from the loop script's own source text*, and the
ledger only gets to say why a derived difference is acceptable. A gate added to
the loop path tomorrow lands in the derived set tomorrow, with nobody
remembering to add it anywhere, and fails this guard until someone either wires
it into the manual path or writes down why not.

What is derived, and how
------------------------
Two surfaces, both read out of ``scripts/loop-phased-step5.sh`` with comments
stripped:

* **Pass-label gates** — every label literal the loop reads through
  ``_has_label "$PR_NUM" "<label>"``. This is deliberately every read in the
  file rather than only the ones inside the merging phase: a gate that *moves*
  must not be able to leave the subject set by moving. It over-reports (a
  routing-only read counts too), which is the safe direction — over-reporting
  costs a ledger line, under-reporting costs a silent divergence.

  Reading ``_has_label`` rather than the ``_REVIEW_PASS_LABELS`` array is also
  what keeps ``acceptance-passed`` honest. It is in that array — it is
  invalidated on a force-push like its siblings — but no gate on either path
  reads it; the real veto is ``acceptance-failed`` through
  ``_check_nack_labels``. A subject set taken from the array would call it a
  required gate, which is simply false.

* **NACK labels** — the ``MERGE_GATE_NACK_LABELS`` array in
  ``scripts/lib/merge-gate-labels.sh``. These are hard gates in the fail-closed
  direction: any one of them present blocks the merge. It lived in the loop
  script until D#2455 moved it to a library both merge paths read; the array is
  still the subject set, it is now read from where it is defined.

A label is "enforced on the manual path" when its literal appears in
``scripts/merge-and-hook.sh`` with comments stripped, **or** when that file
iterates a shared array from ``merge-gate-labels.sh`` that contains it. The
second clause is new with D#2455 and is what makes a shared definition
expressible here at all: the manual path enforces nine labels by iterating two
arrays, and spells none of them out, so a literal-only check would report nine
unrecorded divergences against a file that enforces every one of them.

The subject is still those two files and **not** the other libraries the
wrapper sources: ``pr-dependents.sh`` mentions ``code-review-passed`` while
doing something unrelated to any gate, and counting that as enforcement is how
a guard ends up certifying parity that does not exist. ``merge-gate-labels.sh``
is different in kind — it is the definition itself, and the wrapper naming one
of its arrays is a gate, not a passing mention.

The ledger, and what stops it going stale
-----------------------------------------
``scripts/ci/merge-gate-parity-ledger.json`` records each difference with a
reason. Four things keep it honest, and each is checked here rather than left
to good intentions:

1. **A derived difference that no entry claims fails the build**, naming the
   label. This is the half that finds *unrecorded* divergence.
2. **An entry claiming a label the manual path now enforces fails**, as a stale
   claim. This is what retires an entry when the gap is actually closed — it is
   how ``browser-test-passed`` left this ledger when the manual path grew the
   gate, rather than sitting here forever describing a fixed defect.
3. **An entry claiming a label the loop no longer gates fails**, the same way.
4. **Every entry carries assertions** — ``present``/``absent`` patterns against
   named files, evaluated on every run. An entry whose evidence stopped
   matching is a claim about a codebase that no longer exists, and it fails.
   An entry that names no labels (a mechanism difference rather than a label
   one) **must** carry at least one, so that no entry can be unfalsifiable
   prose.

On top of that, ``loop_gate_surface`` pins the derived surface itself: the
per-label ``_has_label`` read counts and the NACK list. The counts have no
semantic meaning and are not trying to have one — they are a tripwire. A label
already enforced on both paths can still diverge by the loop growing a *new
condition* on it (the live security trigger is precisely that shape), and a
presence check cannot see that. A changed count can. It costs a one-line
reviewable edit whenever the loop's use of a label changes, and in exchange a
new condition cannot land unnoticed.

What this does NOT check
------------------------
It reads source text. It does not execute either merge path, and it cannot tell
whether a gate that is present actually works — ``tests/test_merge_and_hook.sh``
is what drives the real script. It also cannot see a difference that leaves no
textual trace on either side. Those are the limits; they are why the mechanism
entries in the ledger carry explicit assertions instead of being inferred.

Run from anywhere:

    python3 scripts/ci/merge-gate-parity-guard.py           # reconcile, exit 0/1
    python3 scripts/ci/merge-gate-parity-guard.py --table    # print the parity table

Runs in ``backend (import-smoke)`` via ``scripts/ci/run-guards.sh``, which
discovers this directory. A non-zero exit fails that step, which fails that job,
whose name is one of the exact strings in ``CI_REQUIRED_CHECKS``
(``scripts/lib/ci-status-check.sh``) — so a failure here blocks the merge gate
on both merge paths.

Exit 0: every derived difference is enforced or ledgered with live evidence.
Exit 1: an unrecorded difference, a stale ledger entry, a moved gate surface,
        or an unreadable ledger.
Exit 2: usage error.
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOOP_PATH = REPO_ROOT / "scripts" / "loop-phased-step5.sh"
MANUAL_PATH = REPO_ROOT / "scripts" / "merge-and-hook.sh"
LABELS_PATH = REPO_ROOT / "scripts" / "lib" / "merge-gate-labels.sh"
LEDGER_PATH = REPO_ROOT / "scripts" / "ci" / "merge-gate-parity-ledger.json"

LEDGER_KEYS = {"note", "loop_gate_surface", "divergences"}
SURFACE_KEYS = {"note_on_counts", "has_label_reads", "nack_labels"}
ENTRY_KEYS = {"labels", "status", "tracked_as", "reason", "assert"}
STATUSES = {"open", "deliberate"}

# `_has_label "$PR_NUM" "code-review-passed"`. The second argument must be a
# literal: the array-driven reads inside _invalidate_stale_pass_labels pass
# "$name", which is a vocabulary sweep and not a gate.
HAS_LABEL_RE = re.compile(r'_has_label\s+"[^"]*"\s+"([A-Za-z0-9][A-Za-z0-9._:-]*)"')


def strip_comments(text: str) -> str:
    """Drop shell comments, keeping `#` that sits inside a quoted string.

    Both directions matter. A whole-line comment naming a label would make an
    unenforced gate read as enforced, which is the dangerous direction — the
    guard would certify parity that does not exist. A `#` inside a quoted
    string is ordinary text (a `D#2332` in a log line, a `#` in a URL) and
    truncating there would silently shorten the subject text instead.
    """
    out_lines = []
    for line in text.splitlines():
        out: list[str] = []
        quote: str | None = None
        for i, ch in enumerate(line):
            if quote is not None:
                out.append(ch)
                if ch == quote and (i == 0 or line[i - 1] != "\\"):
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                out.append(ch)
                continue
            if ch == "#" and (not out or out[-1].isspace()):
                break
            out.append(ch)
        out_lines.append("".join(out))
    return "\n".join(out_lines)


def read_stripped(path: Path) -> tuple[str, list[str]]:
    try:
        return strip_comments(path.read_text(encoding="utf-8")), []
    except OSError as exc:
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        return "", [f"could not read {rel}: {exc}"]


def bash_array(text: str, name: str) -> list[str] | None:
    """The quoted string literals of a `NAME=( ... )` bash array, or None."""
    match = re.search(re.escape(name) + r"=\(\s*(.*?)\n\)", text, re.S)
    if match is None:
        return None
    return re.findall(r'"([^"]+)"', match.group(1))


def mentions(label: str, text: str) -> bool:
    """True when `label` appears in `text` as a whole token.

    Substring matching would be wrong in both directions here: `wip` occurs
    inside ordinary words, and `security-review-passed` shares a prefix with
    `security-review-needs-fix`.
    """
    pattern = r"(?<![A-Za-z0-9._-])" + re.escape(label) + r"(?![A-Za-z0-9._-])"
    return re.search(pattern, text) is not None


def load_ledger(path: Path) -> tuple[dict, list[str]]:
    """Return (ledger, hard errors). Errors mean the ledger is unusable."""
    if not path.exists():
        return {}, [f"{path.relative_to(REPO_ROOT)} is missing — nothing records the differences"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.relative_to(REPO_ROOT)} is not readable JSON: {exc}"]

    rel = path.relative_to(REPO_ROOT)
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, [f"{rel} must be a JSON object, got {type(raw).__name__}"]
    unknown = sorted(set(raw) - LEDGER_KEYS)
    if unknown:
        errors.append(f"{rel} has unknown top-level key(s): {', '.join(unknown)}")

    surface = raw.get("loop_gate_surface")
    if not isinstance(surface, dict):
        errors.append(f"{rel} is missing its required 'loop_gate_surface' object")
    else:
        unknown = sorted(set(surface) - SURFACE_KEYS)
        if unknown:
            errors.append(f"{rel}: 'loop_gate_surface' has unknown key(s): {', '.join(unknown)}")
        if not isinstance(surface.get("has_label_reads"), dict):
            errors.append(f"{rel}: 'loop_gate_surface.has_label_reads' must be an object of label -> count")
        if not isinstance(surface.get("nack_labels"), list):
            errors.append(f"{rel}: 'loop_gate_surface.nack_labels' must be a list of labels")

    divergences = raw.get("divergences")
    if not isinstance(divergences, dict):
        errors.append(f"{rel} is missing its required 'divergences' object")
        divergences = {}

    for entry_id, entry in sorted(divergences.items()):
        where = f"{rel}: divergences.{entry_id}"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(set(entry) - ENTRY_KEYS)
        if unknown:
            errors.append(f"{where} has unknown key(s): {', '.join(unknown)}")
        labels = entry.get("labels")
        if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
            errors.append(f"{where}.labels must be a list of label strings (use [] for a mechanism difference)")
            labels = []
        if entry.get("status") not in STATUSES:
            errors.append(f"{where}.status must be one of {sorted(STATUSES)}")
        for field in ("tracked_as", "reason"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}.{field} must be a non-empty string")
        asserts = entry.get("assert", [])
        if not isinstance(asserts, list):
            errors.append(f"{where}.assert must be a list")
            asserts = []
        # An entry naming no labels is a claim this guard cannot derive or
        # retire on its own. Without evidence it is prose that outlives the
        # thing it describes, which is the failure mode this file exists to
        # prevent — so it is refused rather than trusted.
        if not labels and not asserts:
            errors.append(
                f"{where} names no labels and carries no assertions — a mechanism "
                f"difference must be falsifiable, or it is prose nothing can retire"
            )
        for i, item in enumerate(asserts):
            if not isinstance(item, dict) or set(item) - {"file", "present", "absent"} or "file" not in item:
                errors.append(f"{where}.assert[{i}] must be {{file, present|absent}}")
                continue
            if ("present" in item) == ("absent" in item):
                errors.append(f"{where}.assert[{i}] needs exactly one of 'present' or 'absent'")

    return raw, errors


def evaluate_assertions(entry_id: str, entry: dict) -> list[str]:
    """Check one entry's evidence against the tree as it stands."""
    failures: list[str] = []
    for item in entry.get("assert", []):
        rel = item["file"]
        path = REPO_ROOT / rel
        if not path.is_file():
            failures.append(f"divergence '{entry_id}' asserts against {rel}, which is not a file")
            continue
        text, read_errors = read_stripped(path)
        if read_errors:
            failures.extend(f"divergence '{entry_id}': {e}" for e in read_errors)
            continue
        for kind in ("present", "absent"):
            if kind not in item:
                continue
            try:
                found = re.search(item[kind], text) is not None
            except re.error as exc:
                failures.append(f"divergence '{entry_id}' has an invalid '{kind}' regex for {rel}: {exc}")
                continue
            if kind == "present" and not found:
                failures.append(
                    f"divergence '{entry_id}' claims {rel} still contains /{item[kind]}/ and it does not "
                    f"— the difference this entry describes has changed shape; re-check it and update or remove the entry"
                )
            if kind == "absent" and found:
                failures.append(
                    f"divergence '{entry_id}' claims {rel} does not contain /{item[kind]}/ and it now does "
                    f"— the entry is stale; if this closed the gap, remove the entry"
                )
    return failures


def make_manual_enforced(manual_text: str, shared: dict[str, list[str]]):
    """Is `label` enforced by scripts/merge-and-hook.sh?

    Two ways, and the second is the point (D#2455). Either the wrapper names
    the label itself, or it iterates a shared array from merge-gate-labels.sh
    that contains it — which is how nine labels are enforced by a file that
    spells out none of them. Requiring the literal would have made a shared
    definition unrepresentable: the fix and the unfixed state would look
    identical to this guard, and it would fail the fix.
    """

    def enforced(label: str) -> bool:
        if mentions(label, manual_text):
            return True
        return any(
            label in members and mentions(array_name, manual_text)
            for array_name, members in shared.items()
        )

    return enforced


def build_table(reads: dict[str, int], nacks: list[str], review_pass: list[str],
                enforced_fn, claimed: dict[str, str]) -> str:
    """The parity table, derived — never hand-maintained (D#2332 item 10)."""
    rows = []
    seen = []
    for label in list(review_pass) + [n for n in nacks if n not in review_pass]:
        if label in seen:
            continue
        seen.append(label)
        count = reads.get(label, 0)
        is_nack = label in nacks
        if is_nack:
            loop = "blocks the merge when present (`_check_nack_labels`, fail-closed)"
        elif count:
            loop = f"read as a gate input ({count} `_has_label` read{'s' if count > 1 else ''})"

        else:
            loop = "**not read as a gate** — invalidated on force-push only"
        if not is_nack and not count:
            manual = "not read — and not a gate on either path"
            deliberate = "n/a — not a gate"
        elif enforced_fn(label):
            manual = "read by `merge-and-hook.sh`"
            deliberate = "no divergence"
        elif label in claimed:
            manual = f"**not read** — ledgered as `{claimed[label]}`"
            deliberate = "see ledger entry"
        else:
            manual = "**not read** — UNLEDGERED"
            deliberate = "**unrecorded — this guard fails**"
        rows.append(f"| `{label}` | {loop} | {manual} | {deliberate} |")
    header = (
        "| label | loop path (`loop-phased-step5.sh`) | manual path (`merge-and-hook.sh`) | recorded divergence |\n"
        "|---|---|---|---|"
    )
    return header + "\n" + "\n".join(rows)


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--table"):
        print(f"usage: {Path(sys.argv[0]).name} [--table]", file=sys.stderr)
        return 2

    failures: list[str] = []
    loop_text, errs = read_stripped(LOOP_PATH)
    failures.extend(errs)
    manual_text, errs = read_stripped(MANUAL_PATH)
    failures.extend(errs)

    labels_text, errs = read_stripped(LABELS_PATH)
    failures.extend(errs)

    reads = dict(collections.Counter(HAS_LABEL_RE.findall(loop_text)))
    nacks = bash_array(labels_text, "MERGE_GATE_NACK_LABELS")
    required = bash_array(labels_text, "MERGE_GATE_REQUIRED_PASS_LABELS")
    review_pass = bash_array(loop_text, "_REVIEW_PASS_LABELS")

    for name, value, where in (
        ("MERGE_GATE_NACK_LABELS", nacks, "scripts/lib/merge-gate-labels.sh"),
        ("MERGE_GATE_REQUIRED_PASS_LABELS", required, "scripts/lib/merge-gate-labels.sh"),
        ("_REVIEW_PASS_LABELS", review_pass, "scripts/loop-phased-step5.sh"),
    ):
        if value is None:
            failures.append(
                f"could not find the {name} array in {where} — the merge gate's "
                f"vocabulary is where this guard's subject set comes from, and without it "
                f"a green run would mean nothing"
            )
    nacks = nacks or []
    required = required or []
    review_pass = review_pass or []
    shared_arrays = {
        "MERGE_GATE_NACK_LABELS": nacks,
        "MERGE_GATE_REQUIRED_PASS_LABELS": required,
    }
    manual_enforced = make_manual_enforced(manual_text, shared_arrays)

    # An empty subject set is the silent pass this guard exists to prevent: it
    # would reconcile nothing and report parity.
    if not reads:
        failures.append(
            "derived zero pass-label gates from scripts/loop-phased-step5.sh — a parity check "
            "with no subjects cannot vouch for anything"
        )

    ledger, ledger_errors = load_ledger(LEDGER_PATH)
    failures.extend(ledger_errors)
    divergences = ledger.get("divergences", {}) if not ledger_errors else {}
    surface = ledger.get("loop_gate_surface", {}) if not ledger_errors else {}

    # Which entry claims which label. A label claimed twice is ambiguous about
    # which reason applies to it, so it is refused rather than resolved.
    claimed: dict[str, str] = {}
    for entry_id, entry in sorted(divergences.items()):
        for label in entry.get("labels", []):
            if label in claimed:
                failures.append(
                    f"label '{label}' is claimed by both divergence '{claimed[label]}' and "
                    f"'{entry_id}' — one difference, one reason"
                )
                continue
            claimed[label] = entry_id

    if not ledger_errors:
        # Tripwire on the derived surface itself. See the module docstring: a
        # label already enforced on both paths can still diverge by the loop
        # growing a new condition on it, which no presence check can see.
        pinned_reads = surface.get("has_label_reads", {})
        for label in sorted(set(reads) | set(pinned_reads)):
            got, want = reads.get(label), pinned_reads.get(label)
            if got == want:
                continue
            if want is None:
                failures.append(
                    f"scripts/loop-phased-step5.sh now reads '{label}' via _has_label ({got}x) and "
                    f"loop_gate_surface.has_label_reads does not mention it — a new gate on the loop "
                    f"path. Wire it into scripts/merge-and-hook.sh or ledger the difference, then pin it here"
                )
            elif got is None:
                failures.append(
                    f"loop_gate_surface.has_label_reads pins '{label}' at {want} read(s) but "
                    f"scripts/loop-phased-step5.sh no longer reads it — the pin is stale"
                )
            else:
                failures.append(
                    f"scripts/loop-phased-step5.sh reads '{label}' {got}x, pinned at {want}x. The loop's "
                    f"use of this label changed: confirm the manual path does not need the new condition, "
                    f"then update the pin"
                )
        pinned_nacks = surface.get("nack_labels", [])
        if isinstance(pinned_nacks, list) and sorted(pinned_nacks) != sorted(nacks):
            added = sorted(set(nacks) - set(pinned_nacks))
            gone = sorted(set(pinned_nacks) - set(nacks))
            failures.append(
                "_NACK_LABELS has changed since loop_gate_surface.nack_labels was pinned"
                + (f" (added: {', '.join(added)})" if added else "")
                + (f" (removed: {', '.join(gone)})" if gone else "")
                + " — confirm the manual path's handling, then update the pin"
            )

        # The half that finds UNRECORDED divergence.
        for label in sorted(set(reads) | set(nacks)):
            enforced = manual_enforced(label)
            entry_id = claimed.get(label)
            if enforced and entry_id:
                failures.append(
                    f"divergence '{entry_id}' claims scripts/merge-and-hook.sh does not enforce "
                    f"'{label}', but it now mentions it — the gap looks closed. Remove the entry "
                    f"rather than leaving a ledger that describes a fixed defect"
                )
            elif not enforced and not entry_id:
                kind = "NACK label (blocks the loop merge when present)" if label in nacks else "gate label"
                failures.append(
                    f"'{label}' is a {kind} on the loop path and scripts/merge-and-hook.sh does not "
                    f"mention it — an unrecorded divergence. Enforce it on the manual path, or add an "
                    f"entry to scripts/ci/merge-gate-parity-ledger.json saying why the two paths differ"
                )

        for label, entry_id in sorted(claimed.items()):
            if label not in reads and label not in nacks:
                failures.append(
                    f"divergence '{entry_id}' claims label '{label}', which the loop path no longer "
                    f"gates on — the entry is stale"
                )

        for entry_id, entry in sorted(divergences.items()):
            failures.extend(evaluate_assertions(entry_id, entry))

    if len(sys.argv) == 2:
        print(build_table(reads, nacks, review_pass, manual_enforced, claimed))
        return 1 if failures else 0

    if failures:
        for line in failures:
            print(f"merge-gate-parity: FAIL — {line}", file=sys.stderr)
        print(
            f"merge-gate-parity: {len(failures)} problem(s) across "
            f"{len(set(reads) | set(nacks))} gate label(s)",
            file=sys.stderr,
        )
        return 1

    enforced_count = sum(1 for label in set(reads) | set(nacks) if manual_enforced(label))
    print(
        f"merge-gate-parity: OK — {len(set(reads) | set(nacks))} gate label(s) on the loop path "
        f"({enforced_count} also enforced by merge-and-hook.sh, "
        f"{len(claimed)} ledgered as divergent), "
        f"{len(divergences)} divergence entr(y/ies) with live evidence"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
