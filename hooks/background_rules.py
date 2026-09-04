"""hooks/background_rules.py

Pure classifier for backgrounding a job, whether via the `run_in_background`
Bash flag (D#2070) or shell-level backgrounding inside the command string
itself (D#2248).

Nothing in this repo re-invokes a sub-agent when a backgrounded job finishes —
`SubagentStop` fires on agent stop, not job completion, and
`scripts/subagent-stop-hook.sh` is contractually non-blocking. A sub-agent
that backgrounds a job and ends its turn waiting for a notification just
parks forever. This classifier makes that structurally impossible for
non-Team-Lead tiers by denying both the flag and its shell-level equivalents
at the boundary: a trailing `&`, `nohup`, `setsid`, or `disown` park a
sub-agent exactly as the flag does (D#2248 — measured from a worktree cwd,
all four were previously ALLOW).

No subprocess, no file I/O, no env reads — kept pure so it's a fast unit
test rather than an integration test, and so the hub (`hooks/sandbox.py`)
can call it with a plain dict. Reuses the existing tokenizer helpers from
`hooks.sandbox_rules` (heredoc-stripping, punctuation-aware tokenising)
rather than writing a new parser, so quoting/heredoc edge cases stay in
one place.
"""

from __future__ import annotations

from hooks.sandbox_rules import Decision, _strip_heredoc_bodies, _tokenize_punctuation_aware

REASON = (
    "background_run_forbidden: run_in_background is unavailable to sub-agents "
    "— nothing re-invokes you when the job finishes, so the turn ends with no "
    "verdict (D#2070). This also covers shell-level backgrounding spelled "
    "directly in the command string — a trailing `&`, or `nohup`/`setsid`/"
    "`disown` — same trap, different spelling (D#2248). Run it bounded in "
    "the foreground instead: timeout --kill-after=5s <seconds> <command>. If "
    "it genuinely cannot be bounded, stop and report verdict: fail with "
    'block_reason "unbounded_background_run".'
)

# Command names that detach a job from the current shell when they lead a
# command segment (D#2248). Matched as whole tokens in segment-leading
# position only — never as a substring — so `nohup.out` as a filename or
# `nohup` as a grep search pattern never trips this.
_BACKGROUND_LAUNCH_TOKENS = {"nohup", "setsid", "disown"}

# Tokens that end one command segment and start the next. `&` is included
# both as a separator (so a token right after it, e.g. `disown`, is treated
# as segment-leading) and is itself the background operator being detected —
# see _shell_backgrounds below. Only the punctuation-aware tokenizer's exact
# `&` token counts; `&&`, `&>`, and `>&` are distinct tokens from that
# tokenizer and never equal to `&` (Spec criterion 2).
_SEGMENT_SEPARATORS = {";", "&&", "||", "|", "&"}


def _shell_backgrounds(command: str) -> bool:
    """True if *command* backgrounds a job via shell syntax rather than the
    `run_in_background` flag: a bare `&` control operator, or a
    segment-leading `nohup`/`setsid`/`disown`.

    Uses `_tokenize_punctuation_aware` (shlex `punctuation_chars` mode) on
    the heredoc-stripped command, same as `hooks.sandbox_rules` does
    elsewhere — this is what keeps `&&`, `2>&1`, `&>`, and a quoted `&`
    (inside a single word token) from ever being mistaken for the bare `&`
    control operator.

    Falls back to `False` (allow) when the command can't be tokenised
    (unbalanced quoting) — same conservative direction the rest of this
    module's callers already take for untokenisable input.
    """
    if not command:
        return False
    try:
        tokens = _tokenize_punctuation_aware(_strip_heredoc_bodies(command))
    except ValueError:
        return False

    segment_start = True
    for tok in tokens:
        if tok == "&":
            return True
        if tok in _SEGMENT_SEPARATORS:
            segment_start = True
            continue
        if segment_start and tok in _BACKGROUND_LAUNCH_TOKENS:
            return True
        segment_start = False
    return False


def classify_background(tool_input: dict) -> Decision:
    """Deny a Bash call that backgrounds a job, by flag or by shell syntax.

    - Truthy `run_in_background` -> denied (D#2070).
    - A command string containing a bare `&`, or a segment-leading `nohup`,
      `setsid`, or `disown` -> denied (D#2248).
    - Neither present -> allowed.
    """
    if tool_input.get("run_in_background"):
        return Decision(allow=False, reason=REASON)
    if _shell_backgrounds(tool_input.get("command", "")):
        return Decision(allow=False, reason=REASON)
    return Decision(allow=True, reason="")
