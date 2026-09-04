"""backend/spawn_var_contract.py — enforce that every {{var}} a spawn template
references resolves to non-empty content, unless explicitly excused.

D#1788: `--pr` was silently dropped on the floor between `spawn-agent.sh` and
the template renderer because nothing checked whether a variable a template
*referenced* actually had a supplier. `REQUIRED_VARS` in `spawn_templates.py`
was supposed to be that check, but it is a hand-maintained list that had
already drifted for 5 of the 8 PR-scoped roles (three had no entry naming
`pr_number` at all, two had no entry whatsoever) — see D#1788 Probe 4. A
list a human has to remember to update is exactly how the original bug
happened; this module keys off what the template text actually contains
instead.

Every `{{var}}` reference in a rendered template is one of two things:
  - LIVE: a caller supplies it and it resolves to non-empty content.
  - EXCUSED: `RENDER_EMPTY_BY_DESIGN` names it and says why it's fine that
    it renders empty (e.g. it's appended separately elsewhere in the
    assembled prompt, or no supplier exists yet and that's tracked as its
    own follow-up).

Anything else — referenced, unexcused, and empty — is a bug: a placeholder
with no supplier that silently renders blank. `assert_all_referenced_vars_supplied`
raises for that case instead of letting it through.
"""

from __future__ import annotations

from typing import Mapping

# Reuse spawn_templates' own pattern rather than a second copy — a duplicated
# regex is exactly how the two can silently drift apart (this repo has three
# open Discussions about that pattern: D#1941, D#1951, D#1953). Deferred
# import: spawn_templates.py imports this module lazily inside render()/
# render_body(), so importing spawn_templates at our own module level here
# does not create a load-time cycle.
from backend.spawn_templates import _VAR_RE  # noqa: F401 (re-exported for callers)


class SpawnVarContractError(ValueError):
    """Raised when a template references a variable with no supplier and no excuse."""


# One entry per variable that is allowed to render empty, with a one-line
# reason. Keep this list honest — adding an entry here silences the loud
# failure for that variable across every role that references it, so it
# should only ever be used for a variable that genuinely has no supplier
# yet (tracked as its own follow-up) or is appended to the prompt separately
# by prompt_builder.render() rather than substituted inline.
RENDER_EMPTY_BY_DESIGN: dict[str, str] = {
    # Appended separately by prompt_builder.render() as their own sections —
    # the content DOES reach the agent, just later in the assembled prompt
    # rather than inline in the template slot. Misleading structure, not
    # lost information (D#1788 Scope decisions).
    "working_principles": "appended separately by prompt_builder.render() as its own section",
    "gate_context": "appended separately by prompt_builder.render() as the gate_line section",
    "self_observe_gate": "appended separately by prompt_builder.render() as its own section",
    "persona_voice": "appended separately by prompt_builder.render() as its own section",
    # No supplier yet — each needs its own data source and its own Discussion
    # (D#1788 Scope decisions: explicitly OUT of this Spec).
    "discussion_title": "needs backend.discussion_cache (D#1783); follow-up Discussion",
    "project_context": "no supplier yet; sizing it is its own Discussion",
    "agent_memory": "no supplier yet; sizing it is its own Discussion",
    "release_id": "runbook-writer/release-manager specific; needs its own data source",
    "trigger_type": "incident-commander specific; needs its own data source",
    "evidence_json": "incident-commander specific; needs its own data source",
    "tour_goal": "browser-tester specific; needs its own data source",
    "affected_pages": "browser-tester specific; needs its own data source",
    "trigger": "browser-tester specific; needs its own data source",
    "report_to": "optional notify target; empty renders gracefully",
    # D#1788 fix-round: --discussion is optional at the scripts/spawn-agent.sh
    # argument-parser level (only --role/--task-prompt are required), and
    # several existing callers rely on that — e.g. scripts/replay-debater.sh
    # spawns debater with no --discussion at all. Forcing discussion_number/
    # discussion_url to always be non-empty would have made this contract a
    # second, uncoordinated global constraint on every caller in the repo
    # (measured: 21 of 24 roles raise with discussion=None, vs 0 of 24 on
    # main). Auditing and updating every spawn-agent.sh call site to always
    # pass --discussion is real work but a separate Discussion from the
    # pr_number bug this module exists to fix — tracked as a follow-up.
    "discussion_number": "optional at the CLI level; not every spawn is Discussion-scoped (follow-up to make this a hard requirement)",
    "discussion_url": "optional at the CLI level; not every spawn is Discussion-scoped (follow-up to make this a hard requirement)",
}


def referenced_vars(template_text: str) -> set[str]:
    """Return the set of {{var}} names referenced in *template_text*.

    *template_text* should be post-{{include:...}}-expansion so fragment
    content is covered too, and pre-{{var}}-substitution so the tokens are
    still literally present to scan for.
    """
    return set(_VAR_RE.findall(template_text))


def assert_all_referenced_vars_supplied(
    role: str,
    template_text: str,
    resolved_vars: Mapping[str, str],
) -> None:
    """Raise SpawnVarContractError if *template_text* references a variable that
    resolves to empty in *resolved_vars* and is not excused by RENDER_EMPTY_BY_DESIGN.

    Parameters
    ----------
    role:
        Role name, used only to make the error message actionable.
    template_text:
        The template body after {{include:...}} expansion, before {{var}}
        substitution.
    resolved_vars:
        The full variable map (name -> value) that substitution will use.
        A name absent from this mapping is treated the same as one present
        with an empty value.
    """
    def _is_empty(value: object) -> bool:
        # Coerce before .strip() — a caller may reasonably pass a non-str
        # value (e.g. pr_number=1786 as an int); resolved_vars is typed
        # Mapping[str, str] but nothing enforces that at the call boundary.
        # None must stay "empty", not become the literal string "None".
        if value is None:
            return True
        return not str(value).strip()

    offending = [
        name
        for name in sorted(referenced_vars(template_text))
        if name not in RENDER_EMPTY_BY_DESIGN and _is_empty(resolved_vars.get(name, ""))
    ]
    if offending:
        raise SpawnVarContractError(
            f"role '{role}': template references variable(s) with no supplier and no "
            f"RENDER_EMPTY_BY_DESIGN excuse, resolving to empty: {', '.join(offending)}"
        )
