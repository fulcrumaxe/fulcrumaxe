#!/usr/bin/env bash
# scripts/lib/muse-spawn-prompt.sh — Assemble a Muse-Code spawn prompt.
#
# Muse-native counterpart to scripts/spawn-agent.sh (which targets Claude
# Code's Agent() tool). Renders role card + spawn template + brief into a
# single prompt printed on stdout, with Claude-only directives stripped or
# rewritten for the Muse plane.
#
# Usage:
#   bash scripts/lib/muse-spawn-prompt.sh \
#     --role <role> \
#     --brief "<discussion brief / task text>" \
#     --event-id <role>-<discussion>-<unix-ts> \
#     [--discussion <N>]
#
# --event-id is REQUIRED and must be stable: generate it once per spawn and
# reuse the same value on every resume/fix round so pre-spawn-check and
# post-agent-hook dedup idempotently. Missing --event-id fails loudly.
#
# No network, no gh, no claude calls. Reads only local files.
#
# Placeholder convention (mirrors backend/spawn_templates.render_body):
#   {{task_brief}} / {{discussion_number}} / {{discussion_url}} /
#   {{discussion_title}} / {{CODE_REPO}} are substituted (see below for the
#   Muse-lane supplier of each). {{include:name}} directives are NOT expanded
#   (see _render_template — sanitizing fragments would rewrite their
#   `claude`-binary forbidden-lists onto the `muse` CLI itself); they become
#   loud file-pointer markers. Anything still {{...}} afterwards becomes
#   [MUSE:unresolved:NAME] — loud and brace-free, never a silent blank.
#
# Output contract:
#   - Exit 0 + assembled prompt on stdout → prompt is ready
#   - Exit 1 + error on stderr           → missing arg or missing input file

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/lib/working-principles.sh
source "$SCRIPT_DIR/working-principles.sh"

ROLE=""
BRIEF=""
EVENT_ID=""
DISCUSSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)       ROLE="$2";       shift 2 ;;
    --brief)      BRIEF="$2";      shift 2 ;;
    --event-id)   EVENT_ID="$2";   shift 2 ;;
    --discussion) DISCUSSION="$2"; shift 2 ;;
    *)
      echo "Error: unknown argument: $1" >&2
      echo "Usage: $0 --role <role> --brief <text> --event-id <id> [--discussion <N>]" >&2
      exit 1
      ;;
  esac
done

[[ -z "$ROLE" ]] && { echo "Error: --role is required" >&2; exit 1; }
[[ -z "$BRIEF" ]] && { echo "Error: --brief is required" >&2; exit 1; }
if [[ -z "$EVENT_ID" ]]; then
  echo "Error: --event-id is required (stable <role>-<discussion>-<unix-ts>; reuse it on every resume/fix round)" >&2
  exit 1
fi

CARD="$REPO_ROOT/.claude/agents/${ROLE}.md"
TMPL="$REPO_ROOT/backend/spawn_templates/${ROLE}.tmpl"
[[ -f "$CARD" ]] || { echo "Error: role card not found: $CARD" >&2; exit 1; }
[[ -f "$TMPL" ]] || { echo "Error: spawn template not found: $TMPL" >&2; exit 1; }

# ── muse_sanitize ─────────────────────────────────────────────────────────────
# Rewrite Claude-only directives for the Muse plane:
#   - Agent()/spawn-agent.sh spawning → implement directly (no Claude-native spawn here)
#   - $CLAUDE_PROJECT_DIR           → repo checkout root ($REPO_ROOT)
#   - Discussion URLs/prose         → bare `Closes D#n` (Discussion plane is private)
#   - private Discussion-plane slug → public repo (code plane is this checkout)
#   - `claude` CLI                  → `muse` CLI (no claude binary in Muse sessions)
muse_sanitize() {
  sed -e 's|scripts/spawn-agent\.sh|scripts/lib/muse-spawn-prompt.sh|g' \
      -e 's|Agent(subagent_type=[^)]*)|implement directly|g' \
      -e 's|Agent()/spawn-agent\.sh|direct implementation|g' \
      -e 's|\$CLAUDE_PROJECT_DIR|'"$REPO_ROOT"'|g' \
      -e 's|https://github\.com/[^[:space:]]*/discussions/\([0-9][0-9]*\)|Closes D#\1|g' \
      -e 's|autonomous-agent-7/fulcrumaxe|fulcrumaxe/fulcrumaxe (public repo — the only plane)|g' \
      -e 's|repository(owner:"autonomous-agent-7", name:"fulcrumaxe")|repository(owner:"fulcrumaxe", name:"fulcrumaxe")|g' \
      -e 's|owner:\\"autonomous-agent-7\\"|owner:\\"fulcrumaxe\\"|g' \
      -e 's|\bclaude\b|muse|g'
}

# ── muse_sanitize_brief ───────────────────────────────────────────────────────
# The brief is untrusted Discussion Spec prose. It takes the SAME five
# translation classes as card/template (via muse_sanitize — D#2601 finding 1:
# it used to be printed raw), PLUS command-substitution neutralization:
# a hostile brief must not smuggle $(...) or `...` into agent instructions
# for a downstream shell to run. Card/template keep their legitimate $(...)
# snippets; only the brief is neutralized. Idempotent with muse_sanitize.
muse_sanitize_brief() {
  muse_sanitize | sed -e 's/\$(/(/g' -e "s/\`/'/g"
}

# ── _muse_marker_sweep ────────────────────────────────────────────────────────
# Last-resort loud marker: any {{name}} that survived substitution is a
# template variable this lane has no supplier for. Emit
# [MUSE:unresolved:NAME] — greppable, brace-free (a literal {{...}} in
# output fails tests/test_muse_spawn_prompt.sh), never a silent empty
# (same fail-loud shape as spawn-agent.sh's {{pr_branch}} hard-fail).
_muse_marker_sweep() {
  sed -E -e 's/\{\{([A-Za-z_][A-Za-z0-9_.:-]*)\}\}/[MUSE:unresolved:\1]/g'
}

# ── _esc_subst ──────────────────────────────────────────────────────────────
# Escape a value for the replacement position of bash ${var//pat/rep}:
# `&` there means "the matched text" (and `\` quotes the next char), so an
# unescaped `&&` (CODE_REPO snippet) or `&` (brief prose like "R&D") would
# re-insert the matched token. Backslashes first, then ampersands.
_esc_subst() {
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//&/\\&}"
  printf '%s' "$v"
}

# ── _render_template ──────────────────────────────────────────────────────────
# Fill the role template's {{...}} slots the way the Claude lane's
# render_body() does, minus the suppliers this lane cannot have (no network,
# no control plane, no Discussion read):
#   {{task_brief}}        ← sanitized --brief (same var name as render_body)
#   {{discussion_number}} ← --discussion ("" when omitted: render_body's own
#                           graceful-empty default for this var)
#   {{discussion_title}}  ← NO supplier (needs a Discussion read) → marker
#   {{discussion_url}}    ← NEVER the private URL; privacy-preserving mirror
#                           is the bare `Closes D#n` form (withheld-marker
#                           when --discussion was omitted)
#   {{CODE_REPO}}         ← NEVER a pinned slug (the card forbids pinning the
#                           code plane); mirror is the repo-resolve snippet the
#                           card already teaches, valid inline in --repo flags
#   {{project_context}} / {{agent_memory}} / {{gate_context}} /
#   {{working_principles}} / {{self_observe_gate}} ← no Muse-lane supplier →
#                           loud markers with the closest local equivalent
#   {{include:name}}      ← NOT expanded. Fragments list the `claude` binary
#                           in forbidden-command inventories; running them
#                           through muse_sanitize's claude→muse rule would
#                           rewrite the ban onto the `muse` CLI itself. The
#                           marker points at the file in the checkout (local,
#                           no leak) so nothing is lost silently.
# Reads the template on stdin, prints the rendered body on stdout.
_render_template() {
  local text
  text="$(cat)"

  # Park {{include:name}} directives as ASCII-safe numeric tokens BEFORE
  # sanitizing: at least one fragment name contains the word `claude`
  # (hard-stop-no-claude), which muse_sanitize would otherwise rewrite
  # inside the token and detach it from its file.
  local -a inc_names=()
  local tok name i=0
  while IFS= read -r tok; do
    [[ -z "$tok" ]] && continue
    name="${tok#\{\{include:}"; name="${name%\}\}}"
    inc_names+=("$name")
    text="${text//$tok/@@MUSEINCLUDE${i}@@}"
    i=$((i + 1))
  done < <(grep -o '{{include:[^}]*}}' <<<"$text" | sort -u || true)

  # Known suppliers (all values are sanitize-safe literals by construction).
  local safe_brief discussion_ref title_marker code_repo_snippet
  safe_brief="$(printf '%s' "$BRIEF" | muse_sanitize_brief)"
  if [[ -n "$DISCUSSION" ]]; then
    discussion_ref="Closes D#$DISCUSSION"
  else
    discussion_ref="[MUSE:discussion-withheld -- Discussion plane is private; no --discussion given]"
  fi
  title_marker="[MUSE:no-supplier:discussion_title -- reader has the Closes reference only]"
  code_repo_snippet='$(source scripts/lib/repo-resolve.sh && _resolve_code_repo)'
  # shellcheck disable=SC2312
  text="${text//'{{task_brief}}'/$(_esc_subst "$safe_brief")}"
  text="${text//'{{discussion_number}}'/$(_esc_subst "$DISCUSSION")}"
  text="${text//'{{discussion_url}}'/$(_esc_subst "$discussion_ref")}"
  text="${text//'{{discussion_title}}'/$(_esc_subst "$title_marker")}"
  text="${text//'{{CODE_REPO}}'/$(_esc_subst "$code_repo_snippet")}"
  text="${text//'{{project_context}}'/$(_esc_subst "[MUSE:no-supplier:project_context -- work from the checkout tree]")}"
  text="${text//'{{agent_memory}}'/$(_esc_subst "[MUSE:no-supplier:agent_memory -- no carry-over memory in this lane]")}"
  text="${text//'{{gate_context}}'/$(_esc_subst "[MUSE:no-supplier:gate_context -- run scripts/pre-spawn-check.sh per AGENTS.md]")}"
  text="${text//'{{working_principles}}'/$(_esc_subst "[MUSE: working principles are appended under ## Working Principles below]")}"
  text="${text//'{{self_observe_gate}}'/$(_esc_subst "[MUSE:no-supplier:self_observe_gate]")}"

  # Sanitize the assembled body (brief arrives pre-sanitized; muse_sanitize
  # is idempotent over its own output), then restore include markers
  # post-sanitize so filenames survive verbatim.
  text="$(printf '%s' "$text" | muse_sanitize)"
  local idx marker
  for idx in "${!inc_names[@]}"; do
    marker="[MUSE:include-fragment:${inc_names[$idx]} -- read backend/spawn_templates/fragments/${inc_names[$idx]}.md in your checkout]"
    text="${text//@@MUSEINCLUDE${idx}@@/$(_esc_subst "$marker")}"
  done

  printf '%s' "$text" | _muse_marker_sweep
}

{
  echo "## ROLE: $ROLE"
  echo ""
  muse_sanitize < "$CARD" | _muse_marker_sweep
  echo ""
  echo "## TEMPLATE"
  echo ""
  _render_template < "$TMPL"
  echo ""
  echo "## BRIEF"
  echo ""
  printf '%s\n' "$BRIEF" | muse_sanitize_brief | _muse_marker_sweep
  if [[ -n "$DISCUSSION" ]]; then
    echo ""
    echo "Discussion reference: bare \`Closes D#$DISCUSSION\` form only — never paste Discussion URLs or Spec prose outward (private). Restate findings in your own words against the code."
  fi
  echo ""
  working_principles_block
  echo ""
  echo "## REPORTING"
  echo ""
  echo "End your final message with a JSON envelope in \`<!-- AGENT_OUTPUT -->\` markers."
  echo "The tokens_used self-report is MANDATORY — always include it with your best"
  echo "available counts (input/output); omit it only if you genuinely cannot read them."
  echo ""
  echo '<!-- AGENT_OUTPUT -->'
  echo '```json'
  echo '{'
  echo '  "agent": "'"$ROLE"'",'
  echo '  "verdict": "done|fail",'
  echo '  "tokens_used": {"input": 0, "output": 0}'
  echo '}'
  echo '```'
  echo '<!-- /AGENT_OUTPUT -->'
  echo ""
  echo "hook_event_id=$EVENT_ID"
}
