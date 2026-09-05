#!/usr/bin/env bash
# Integration test: PM consensus panel triggering rule.
# HARD RULE: Never spawns real agents, calls `claude`, or triggers /loop.
# All specialist responses are mocked inline.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0

ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
err() { echo "  FAIL: $1 (got '$2', expected '$3')"; FAIL=$((FAIL+1)); }

check() { [ "$2" = "$3" ] && ok "$1" || err "$1" "$2" "$3"; }
has()   { echo "$2" | grep -q "$3" && ok "$1" || { echo "  FAIL: $1 (pattern '$3' missing)"; FAIL=$((FAIL+1)); }; }

echo "=== test_pm_consensus.sh ==="

# 1. Critical panel
echo "--- 1. Critical tag ---"
J=$(python3 "$REPO_ROOT/backend/consensus_panel.py" get-panel --title "[Critical] PM consensus")
check "tag=critical"    "$(echo "$J" | python3 -c 'import sys,json; print(json.load(sys.stdin)["tag"])')"       "critical"
check "mandatory=True"  "$(echo "$J" | python3 -c 'import sys,json; print(json.load(sys.stdin)["mandatory"])')" "True"
check "3 specialists"   "$(echo "$J" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["specialists"]))')" "3"
has "has technical-architect" "$J" "technical-architect"
has "has security-expert"     "$J" "security-expert"
has "has cost-analyst"        "$J" "cost-analyst"

# 2. Feature panel
echo "--- 2. Feature tag ---"
J=$(python3 "$REPO_ROOT/backend/consensus_panel.py" get-panel --title "[Feature] Add KPI tiles")
check "feature mandatory" "$(echo "$J" | python3 -c 'import sys,json; print(json.load(sys.stdin)["mandatory"])')" "True"
has "has product-owner"      "$J" "product-owner"
has "has performance-expert" "$J" "performance-expert"

# 3. Small/Bug/Doc → no panel
echo "--- 3. Small/Bug/Doc: empty panel ---"
for tag in Small Bug Doc; do
  J=$(python3 "$REPO_ROOT/backend/consensus_panel.py" get-panel --title "[$tag] test")
  check "$tag: 0 specialists" \
    "$(echo "$J" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["specialists"]))')" "0"
done

# 4. Budget guardrail
echo "--- 4. Budget ---"
check "budget allowed at 0" \
  "$(python3 "$REPO_ROOT/backend/consensus_panel.py" check-budget --panel "[Critical] test" --spent 0 \
     | python3 -c 'import sys,json; print(json.load(sys.stdin)["allowed"])')" "True"
python3 "$REPO_ROOT/backend/consensus_panel.py" check-budget --panel "[Critical] test" --spent 999999 \
  > /dev/null 2>&1 && { echo "  FAIL: should exit 1 when over cap"; FAIL=$((FAIL+1)); } \
  || ok "budget exits 1 when over cap"

# 5. Mocked specialist parsing
echo "--- 5. Mocked parsing ---"
RESULT=$(python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
from backend.consensus_panel import parse_specialist_output
raw = "### perspective\nModular design recommended.\n\n### concerns\nMissing error handling.\n\n### questions\nShould X be async?\n"
out = parse_specialist_output("technical-architect", 446, 1, raw)
print("perspective_ok:", "Modular" in out.perspective)
print("concerns_ok:", "error" in out.concerns)
print("questions_ok:", "async" in out.questions)
EOF
)
check "perspective parsed" "$(echo "$RESULT" | grep perspective_ok | awk '{print $2}')" "True"
check "concerns parsed"    "$(echo "$RESULT" | grep concerns_ok    | awk '{print $2}')" "True"
check "questions parsed"   "$(echo "$RESULT" | grep questions_ok   | awk '{print $2}')" "True"

# 6. round2_needed
echo "--- 6. round2_needed ---"
R2=$(python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
from backend.consensus_panel import SpecialistOutput, round2_needed
print("with_q:", round2_needed([SpecialistOutput("ta", 1, 1, questions="Should X be Y?")]))
print("no_q:",   round2_needed([SpecialistOutput("ca", 1, 1, questions="")]))
EOF
)
check "round2 True when questions"  "$(echo "$R2" | grep with_q | awk '{print $2}')" "True"
check "round2 False when no questions" "$(echo "$R2" | grep no_q | awk '{print $2}')" "False"

# 7. Consensus summary has ≥2 specialists
echo "--- 7. Consensus summary ---"
BLOCK=$(python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
from backend.consensus_panel import ConsensusResult, SpecialistOutput
r = ConsensusResult(446, "critical", ["technical-architect", "security-expert"], 1,
    outputs=[SpecialistOutput("technical-architect", 446, 1, perspective="Use modular design."),
             SpecialistOutput("security-expert", 446, 1, perspective="Sanitize Discussion body.")])
print(r.to_summary_block())
EOF
)
has "header present"           "$BLOCK" "### Consensus Summary"
has "technical-architect"      "$BLOCK" "technical-architect"
has "security-expert"          "$BLOCK" "security-expert"
has "Round 2 run: No"          "$BLOCK" "Round 2 run: No"

# 8. project-manager.tmpl has consensus keywords
echo "--- 8. PM template ---"
TMPL="$REPO_ROOT/backend/spawn_templates/project-manager.tmpl"
check "tmpl exists" "$(test -f "$TMPL" && echo yes)" "yes"
has "Consensus Panel Protocol" "$(cat "$TMPL")" "Consensus Panel Protocol"
has "triggering rule"          "$(cat "$TMPL")" "Critical"
has "Read specialist comments" "$(cat "$TMPL")" "Read specialist comments"
has "Consensus Summary"        "$(cat "$TMPL")" "Consensus Summary"

# 9. All 5 specialist templates exist with required sections
echo "--- 9. Specialist templates ---"
TMPL_DIR="$REPO_ROOT/backend/spawn_templates"
for role in technical-architect product-owner cost-analyst performance-expert security-expert; do
  check "$role.tmpl exists" "$(test -f "$TMPL_DIR/$role.tmpl" && echo yes)" "yes"
  for sec in perspective concerns questions; do
    has "$role has ### $sec" "$(cat "$TMPL_DIR/$role.tmpl" 2>/dev/null)" "### $sec"
  done
done

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
