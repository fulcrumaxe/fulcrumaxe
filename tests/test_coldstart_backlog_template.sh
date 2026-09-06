#!/usr/bin/env bash
# tests/test_coldstart_backlog_template.sh
#
# Covers the epic template (scripts/coldstart-templates/epic/) and the backlog
# beat that uses it (scripts/lib/coldstart-backlog.sh).
#
# The defect being guarded against: coldstart.sh printed a fill-in-the-blank
# epic format inline that had no YAML frontmatter, while
# scripts/import-epic-tasks.py — the only consumer of the backlog — derives
# every Discussion title and every label from frontmatter. An operator who
# followed the on-screen instructions produced files the seeding step could
# not use.
#
# So the load-bearing assertion here is not "the template file exists". It is
# that a copy of the template, filled in, parses through the importer's OWN
# parser and yields the title and labels the importer would create — and that
# the old inline format, run through the same parser, yields nothing.
#
# Run: bash tests/test_coldstart_backlog_template.sh
# Expects: all assertions pass, exit 0

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="$REPO_ROOT/scripts/coldstart-templates/epic"
BACKLOG_LIB="$REPO_ROOT/scripts/lib/coldstart-backlog.sh"
IMPORTER="$REPO_ROOT/scripts/import-epic-tasks.py"

PASS=0
FAIL=0
FIXTURES=()

cleanup() {
  local d
  for d in "${FIXTURES[@]:-}"; do
    [[ -n "$d" && -d "$d" ]] && rm -rf -- "$d"
  done
}
trap cleanup EXIT

ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; [[ $# -gt 1 ]] && echo "        $2"; FAIL=$((FAIL + 1)); }

assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected '$3', got '$2'"; fi
}
assert_contains() {
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1" "missing substring: $2"; fi
}
assert_not_contains() {
  if [[ "$3" != *"$2"* ]]; then ok "$1"; else bad "$1" "unexpected substring: $2"; fi
}
assert_file() {
  if [[ -f "$2" ]]; then ok "$1"; else bad "$1" "not a file: $2"; fi
}
assert_no_file() {
  if [[ ! -e "$2" ]]; then ok "$1"; else bad "$1" "should not exist: $2"; fi
}

mkfixture() { local d; d="$(mktemp -d)"; FIXTURES+=("$d"); echo "$d"; }

# shellcheck source=scripts/lib/coldstart-backlog.sh
source "$BACKLOG_LIB"

echo ""
echo "=== template files are present ==="
assert_file "epic.md template exists" "$TPL_DIR/epic.md"
assert_file "NN.md task template exists" "$TPL_DIR/NN.md"
assert_file "template README exists" "$TPL_DIR/README.md"

echo ""
echo "=== the template ships no internal references ==="
TPL_TEXT="$(cat "$TPL_DIR"/*.md)"
assert_not_contains "no Discussion numbers in the template" "D#" "$TPL_TEXT"
assert_not_contains "no private repo slug in the template" "autonomous-agent-7" "$TPL_TEXT"

echo ""
echo "=== the importer's own parser reads the template ==="
# Loads scripts/import-epic-tasks.py as a module and calls the exact functions
# the real import path uses — not a re-implementation of them.
PARSE_OUT="$(python3 - "$IMPORTER" "$TPL_DIR/NN.md" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("imp", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
fm, _body = m.parse_frontmatter(open(sys.argv[2], encoding="utf-8").read())
if not fm:
    print("PARSE_FAILED")
    sys.exit(0)
labels = []
if fm.get("epic") is not None:
    labels.append(f"epic-{fm['epic']}")
if fm.get("type"):
    labels.append(str(fm["type"]).lower())
if fm.get("estimated_hours") is not None:
    labels.append(f"est-{fm['estimated_hours']}h")
print(json.dumps({
    "fields": sorted(fm.keys()),
    "title": m.format_title(fm),
    "labels": labels,
}, ensure_ascii=False))
PY
)"
assert_not_contains "template frontmatter parses (not PARSE_FAILED)" "PARSE_FAILED" "$PARSE_OUT"
for field in epic task title type status estimated_hours depends_on tags; do
  assert_contains "template carries frontmatter field: $field" "\"$field\"" "$PARSE_OUT"
done
assert_contains "template yields a well-formed Discussion title" "[Feature] epic-1.1 —" "$PARSE_OUT"
assert_contains "template yields the epic label" "\"epic-1\"" "$PARSE_OUT"
assert_contains "template yields the type label" "\"feature\"" "$PARSE_OUT"
assert_contains "template yields the estimate label" "\"est-4h\"" "$PARSE_OUT"

echo ""
echo "=== the format coldstart used to print inline yields nothing ==="
# The measured negative (D#1984): the old on-screen format run through the
# same parser produces no frontmatter at all, so no title and no labels.
OLD_FMT="$(mkfixture)/old-format.md"
cat >"$OLD_FMT" <<'OLD'
# Epic 1: Some title

## Goal
Do the thing.

## Why now
Because.

## Scope
- a

## Out of scope
- b
OLD
OLD_OUT="$(python3 - "$IMPORTER" "$OLD_FMT" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("imp", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
fm, _ = m.parse_frontmatter(open(sys.argv[2], encoding="utf-8").read())
print("FIELDS=" + ",".join(sorted(fm.keys())) if fm else "FIELDS=")
print("TITLE=" + m.format_title(fm))
PY
)"
assert_contains "old inline format parses to zero frontmatter fields" "FIELDS=" "$OLD_OUT"
assert_contains "old inline format degrades to a placeholder title" "epic-?.? — untitled" "$OLD_OUT"

echo ""
echo "=== coldstart.sh no longer restates the format inline ==="
CS_TEXT="$(cat "$REPO_ROOT/scripts/coldstart.sh")"
assert_not_contains "coldstart.sh dropped the '## Why now' inline format" "## Why now" "$CS_TEXT"
assert_not_contains "coldstart.sh dropped the '## Out of scope' inline format" "## Out of scope" "$CS_TEXT"
assert_contains "coldstart.sh sources the backlog module" "lib/coldstart-backlog.sh" "$CS_TEXT"
assert_contains "coldstart.sh passes the resolved backlog dir, not a literal" 'coldstart_backlog_step "$EPICS_DIR"' "$CS_TEXT"

echo ""
echo "=== classify: absent ==="
D="$(mkfixture)"
assert_eq "a missing directory classifies as absent" "$(coldstart_backlog_classify "$D/nope")" "absent"

echo ""
echo "=== classify: empty ==="
D="$(mkfixture)"; mkdir -p "$D/epics"
assert_eq "an empty directory classifies as empty" "$(coldstart_backlog_classify "$D/epics")" "empty"
mkdir -p "$D/epics/epic-1-thing"
assert_eq "epic dirs with no markdown still classify as empty" "$(coldstart_backlog_classify "$D/epics")" "empty"

echo ""
echo "=== classify: conforming ==="
D="$(mkfixture)"; mkdir -p "$D/epics/epic-3-billing"
echo "# Epic 3: Billing" >"$D/epics/epic-3-billing/epic.md"
assert_eq "an epic-*/epic.md classifies as conforming" "$(coldstart_backlog_classify "$D/epics")" "conforming"

D="$(mkfixture)"; mkdir -p "$D/epics/epic-3-billing"
printf -- '---\nepic: 3\ntask: 1\ntitle: "x"\n---\n\n# Task: x\n' >"$D/epics/epic-3-billing/01.md"
assert_eq "a task file with frontmatter classifies as conforming" "$(coldstart_backlog_classify "$D/epics")" "conforming"

echo ""
echo "=== classify: foreign ==="
D="$(mkfixture)"; mkdir -p "$D/epics/epic-3-billing"
cat >"$D/epics/epic-3-billing/01.md" <<'FOREIGN'
# Some other backlog shape

We track work in a table, and the word task: appears in this prose
line to prove the classifier reads only the frontmatter block.
FOREIGN
assert_eq "markdown without frontmatter classifies as foreign" "$(coldstart_backlog_classify "$D/epics")" "foreign"

D="$(mkfixture)"; mkdir -p "$D/epics/stories"
echo "# story one" >"$D/epics/stories/one.md"
assert_eq "a non-epic-* layout classifies as foreign" "$(coldstart_backlog_classify "$D/epics")" "foreign"

echo ""
echo "=== scaffold announces before it writes ==="
D="$(mkfixture)"
OUT="$(coldstart_backlog_step "$D/epics" "$D" "$TPL_DIR" 2>&1)"
assert_contains "step names every path before writing" "About to write, and nothing else" "$OUT"
assert_contains "step names the epic.md it will write" "epic-1-example/epic.md" "$OUT"
assert_contains "step names the task file it will write" "epic-1-example/01.md" "$OUT"
assert_contains "step points at the template README, not an inline format" "README.md" "$OUT"
assert_contains "step warns an unedited placeholder would be seeded" "Edit first" "$OUT"
assert_file "scaffolded epic.md exists" "$D/epics/epic-1-example/epic.md"
assert_file "scaffolded 01.md exists" "$D/epics/epic-1-example/01.md"

echo ""
echo "=== the scaffolded copy is the template, byte for byte ==="
if diff -q "$TPL_DIR/epic.md" "$D/epics/epic-1-example/epic.md" >/dev/null 2>&1; then
  ok "scaffolded epic.md is identical to the template"
else
  bad "scaffolded epic.md is identical to the template" "diff is non-empty"
fi
if diff -q "$TPL_DIR/NN.md" "$D/epics/epic-1-example/01.md" >/dev/null 2>&1; then
  ok "scaffolded 01.md is identical to the template"
else
  bad "scaffolded 01.md is identical to the template" "diff is non-empty"
fi

echo ""
echo "=== the scaffold is idempotent and never overwrites ==="
echo "OPERATOR EDIT" >"$D/epics/epic-1-example/01.md"
OUT="$(coldstart_backlog_step "$D/epics" "$D" "$TPL_DIR" 2>&1)"
assert_eq "an already-scaffolded dir classifies as conforming" "$(coldstart_backlog_classify "$D/epics")" "conforming"
assert_contains "a conforming backlog is reported, not rewritten" "Nothing written" "$OUT"
assert_eq "the operator's edit survived" "$(cat "$D/epics/epic-1-example/01.md")" "OPERATOR EDIT"

echo ""
echo "=== a foreign backlog is reported and left completely alone ==="
D="$(mkfixture)"; mkdir -p "$D/epics/epic-9-legacy"
cat >"$D/epics/epic-9-legacy/work.md" <<'FOREIGN'
# Legacy backlog
- [ ] thing one
- [ ] thing two
FOREIGN
BEFORE="$(find "$D/epics" -type f | sort; md5sum "$D/epics/epic-9-legacy/work.md" 2>/dev/null | cut -d' ' -f1)"
OUT="$(coldstart_backlog_step "$D/epics" "$D" "$TPL_DIR" 2>&1)"
AFTER="$(find "$D/epics" -type f | sort; md5sum "$D/epics/epic-9-legacy/work.md" 2>/dev/null | cut -d' ' -f1)"
assert_eq "no file was added, removed, or changed" "$AFTER" "$BEFORE"
assert_no_file "no example epic was scaffolded over a foreign backlog" "$D/epics/epic-1-example"
assert_contains "the report says nothing was written" "Nothing was written, moved, or deleted" "$OUT"
assert_contains "the report refuses conversion explicitly" "does not convert an existing backlog" "$OUT"
assert_contains "the report offers the --dry-run check" "--dry-run" "$OUT"
assert_contains "the report offers a separate --backlog path as the escape hatch" "--backlog" "$OUT"

echo ""
echo "=== a missing template degrades honestly ==="
# The module ships via the engine-sync allowlist (scripts/lib/*.sh); the
# template directory does not. If a checkout has one without the other, the
# step must say so and must not point at docs that are not there.
D="$(mkfixture)"
OUT="$(coldstart_backlog_step "$D/epics" "$D" "$D/no-such-template" 2>&1)"
assert_contains "a missing template is reported by path" "epic template not found" "$OUT"
assert_not_contains "no next-steps pointing at a README that is not there" "README.md" "$OUT"
assert_no_file "nothing was scaffolded from a template that is not there" "$D/epics/epic-1-example/epic.md"

echo ""
echo "=== --backlog divergence from the seeding path is surfaced ==="
D="$(mkfixture)"
OUT="$(coldstart_backlog_step "$D/other-backlog" "$D" "$TPL_DIR" 2>&1)"
assert_contains "a non-default backlog dir warns about the seeding path" "will not be seeded" "$OUT"
assert_file "the scaffold still respects --backlog, not a hardcoded epics/" "$D/other-backlog/epic-1-example/epic.md"
assert_no_file "nothing was written to the default epics/ path" "$D/epics"

D="$(mkfixture)"
OUT="$(coldstart_backlog_step "$D/epics" "$D" "$TPL_DIR" 2>&1)"
assert_not_contains "the default backlog dir warns about nothing" "will not be seeded" "$OUT"

echo ""
echo "=== shell syntax ==="
if bash -n "$BACKLOG_LIB"; then ok "coldstart-backlog.sh parses"; else bad "coldstart-backlog.sh parses"; fi
if bash -n "$REPO_ROOT/scripts/coldstart.sh"; then ok "coldstart.sh parses"; else bad "coldstart.sh parses"; fi

echo ""
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
