"""
YAML workflow runner -- structured coordination for the autonomous team.

Loads workflow YAML files, validates inputs, interpolates {{variable}} templates,
and returns a resolved execution plan. The runner does NOT spawn agents -- it
just produces the plan. The Team Lead executes the plan step-by-step.

Usage (CLI):
    python backend/workflow_runner.py list
    python backend/workflow_runner.py validate implement-discussion
    python backend/workflow_runner.py resolve implement-discussion \
        --input discussion_number=42 \
        --input discussion_title="Test feature" \
        --input discussion_url="https://github.com/..." \
        --input spec_body="do the thing"

Usage (library):
    from backend.workflow_runner import WorkflowRunner
    runner = WorkflowRunner()
    plan = runner.resolve("implement-discussion", {
        "discussion_number": 42,
        "discussion_title": "Test feature",
        "discussion_url": "https://github.com/...",
        "spec_body": "do the thing",
    })
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# Lazy import — only used when appending mandatory appendices to prompts.
# Kept lazy to avoid circular import issues during tests that stub workflow_runner.
try:
    from backend import spawn_templates as _spawn_templates  # type: ignore[import]
except ImportError:
    try:
        import spawn_templates as _spawn_templates  # type: ignore[import]
    except ImportError:
        _spawn_templates = None  # type: ignore[assignment]


# Root of the workflows directory, relative to repo root.
_DEFAULT_WORKFLOWS_DIR = Path(".autonomous-team/workflows")

# Supported workflow patterns.
_SUPPORTED_PATTERNS = {
    "sequence",
    "parallel",
    "compete",
    "escalation",
    "supervisor",
    "alongside",
    "loop",
    "conditional",
    "delegate",
}

# Maximum recursion depth for delegate pattern.
_MAX_DELEGATE_DEPTH = 5

# Regex matching an interpolation token.
_TOKEN_RE = re.compile(r"\{\{([^}]+)\}\}")


class WorkflowNotFoundError(FileNotFoundError):
    """Raised when a requested workflow does not exist."""


class ValidationError(ValueError):
    """Raised when a workflow file fails validation."""


class MissingInputError(ValueError):
    """Raised when required inputs are not provided."""


class DelegateDepthError(ValueError):
    """Raised when delegate recursion exceeds _MAX_DELEGATE_DEPTH."""


class WorkflowRunner:
    """Load, validate, and resolve YAML workflow definitions."""

    def __init__(self, workflows_dir: Path | str | None = None):
        if workflows_dir is None:
            here = Path(__file__).resolve().parent
            repo_root = here.parent
            self._dir = repo_root / _DEFAULT_WORKFLOWS_DIR
        else:
            self._dir = Path(workflows_dir).resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_workflows(self) -> list[str]:
        """Return names of all available workflows (sorted), including examples subdir."""
        if not self._dir.exists():
            return []
        names = sorted(p.stem for p in self._dir.glob("*.yaml"))
        examples_dir = self._dir / "examples"
        if examples_dir.exists():
            names += sorted(f"examples/{p.stem}" for p in examples_dir.glob("*.yaml"))
        return names

    def validate(self, workflow_name: str) -> list[str]:
        """Validate a workflow. Returns list of error strings (empty = valid)."""
        raw = self._load_raw(workflow_name)
        return _validate_schema(raw, workflow_name)

    def resolve(
        self,
        workflow_name: str,
        context: dict[str, Any],
        _depth: int = 0,
    ) -> list[dict]:
        """Resolve a workflow against the provided input context.

        Dispatches to a pattern-specific resolver. Returns a list of resolved
        step dicts with pattern-specific metadata fields.

        Raises:
          WorkflowNotFoundError - workflow file not found.
          ValidationError       - workflow schema invalid.
          MissingInputError     - required inputs missing from context.
          DelegateDepthError    - delegate recursion depth exceeded.
        """
        if _depth >= _MAX_DELEGATE_DEPTH:
            raise DelegateDepthError(
                f"Delegate recursion depth exceeded {_MAX_DELEGATE_DEPTH} "
                f"while resolving '{workflow_name}'."
            )

        raw = self._load_raw(workflow_name)

        errors = _validate_schema(raw, workflow_name)
        if errors:
            raise ValidationError(
                f"Workflow '{workflow_name}' failed validation:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        str_context = {k: str(v) for k, v in context.items()}

        missing = _check_required_inputs(raw, str_context)
        if missing:
            raise MissingInputError(
                f"Missing required inputs for '{workflow_name}': "
                + ", ".join(sorted(missing))
            )

        pattern = raw.get("pattern", "sequence")
        resolver = _PATTERN_RESOLVERS.get(pattern)
        if resolver is None:
            raise ValidationError(f"No resolver for pattern '{pattern}'.")

        return resolver(raw, str_context, self, _depth)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_raw(self, workflow_name: str) -> dict:
        """Load and parse the YAML file for *workflow_name*."""
        path = self._dir / f"{workflow_name}.yaml"
        if not path.exists():
            raise WorkflowNotFoundError(
                f"Workflow not found: '{workflow_name}' "
                f"(looked in {self._dir})"
            )
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}


# ------------------------------------------------------------------
# Pattern resolvers
# ------------------------------------------------------------------


def _base_step(step: dict, str_context: dict[str, str]) -> dict:
    """Build the common fields for a resolved step.

    The prompt is assembled by interpolating the YAML prompt_template and then
    appending mandatory appendices (repo scope, AGENT_OUTPUT envelope, archive
    protocol, role-specific gate checks) via spawn_templates if available.
    """
    is_conditional = "condition" in step
    prompt_template = step.get("prompt_template", "")
    prompt = _interpolate(prompt_template, str_context)

    # Append mandatory appendices for known agent roles.
    agent = step.get("agent", "")
    if _spawn_templates is not None and agent in _spawn_templates.KNOWN_ROLES:
        prompt = _append_mandatory_appendices(prompt, agent)

    return {
        "id": step["id"],
        "agent": agent,
        "prompt": prompt,
        "expects": step.get("expects", []),
        "timeout_minutes": step.get("timeout_minutes"),
        "conditional": is_conditional,
        "condition": step.get("condition"),
    }


def _append_mandatory_appendices(prompt: str, role: str) -> str:
    """Append the mandatory appendices block to an already-interpolated prompt.

    Uses spawn_templates constants directly to avoid double-loading a .tmpl
    file — the prompt body already came from the YAML prompt_template.
    """
    if _spawn_templates is None:
        return prompt

    envelope = _spawn_templates._ENVELOPE_BY_ROLE.get(role, "")
    gate_checks = _spawn_templates._GATE_CHECKS_BY_ROLE.get(role, "")

    sections = [
        prompt.rstrip(),
        "",
        "---",
        "## Repo Scope (mandatory)",
        _spawn_templates._REPO_SCOPE,
        "",
        "---",
        "## AGENT_OUTPUT Envelope (mandatory)",
        envelope,
        "",
        "---",
        "## Archive Protocol (mandatory)",
        _spawn_templates._ARCHIVE_PROTOCOL,
        "",
        "---",
        "## Control-Plane Gate Checks (mandatory)",
        gate_checks,
    ]
    return "\n".join(sections)


def _resolve_sequence(raw, str_context, runner, _depth):
    """sequence -- steps run in order, one at a time.

    Special-case for review-pr workflow: when a step has a ``condition`` field
    that evaluates to False via the context (e.g. ``dashboard_touched`` is
    falsy), that step is omitted from the resolved plan.  This keeps the YAML
    declarative while the Python side handles the conditional logic.
    """
    resolved = []
    for step in raw.get("steps", []):
        condition = step.get("condition")
        if condition is not None:
            # Evaluate simple truthy conditions against the string context.
            # Supports bare key names ("dashboard_touched") and
            # "key == value" comparisons.
            # None (unevaluable / key absent) → treat as False → skip.
            # This ensures conditional steps are opt-in: they only run when
            # the caller explicitly passes a truthy value.
            evaluated = _try_evaluate_condition(condition, str_context)
            if evaluated is not True:
                # False or None → skip this step.
                continue
        resolved.append(_base_step(step, str_context))
    return resolved


def _resolve_parallel(raw, str_context, runner, _depth):
    """parallel -- all steps run concurrently; Team Lead waits for all."""
    resolved = []
    for step in raw.get("steps", []):
        s = _base_step(step, str_context)
        s["parallel"] = True
        resolved.append(s)
    return resolved


def _resolve_compete(raw, str_context, runner, _depth):
    """compete -- all steps run concurrently; first passing verdict wins."""
    winner_strategy = raw.get("winner_strategy", "first_pass")
    resolved = []
    for step in raw.get("steps", []):
        s = _base_step(step, str_context)
        s["compete"] = True
        s["winner_strategy"] = winner_strategy
        resolved.append(s)
    return resolved


def _resolve_escalation(raw, str_context, runner, _depth):
    """escalation -- steps run in escalation_level order; higher levels are fallbacks."""
    steps = sorted(raw.get("steps", []), key=lambda s: s.get("escalation_level", 0))
    resolved = []
    for i, step in enumerate(steps):
        s = _base_step(step, str_context)
        s["escalation_level"] = step.get("escalation_level", i)
        s["fallback"] = i > 0
        resolved.append(s)
    return resolved


def _resolve_supervisor(raw, str_context, runner, _depth):
    """supervisor -- one step monitors workers; supervisor receives worker outputs."""
    steps = raw.get("steps", [])
    supervisor_ids = [s["id"] for s in steps if s.get("role") == "supervisor"]
    worker_ids = [s["id"] for s in steps if s.get("role") == "worker"]
    resolved = []
    for step in steps:
        s = _base_step(step, str_context)
        role = step.get("role", "worker")
        s["supervisor_role"] = role
        if role == "supervisor":
            s["supervises"] = worker_ids
        else:
            s["supervised_by"] = supervisor_ids[0] if supervisor_ids else None
        resolved.append(s)
    return resolved


def _resolve_alongside(raw, str_context, runner, _depth):
    """alongside -- background step runs in parallel with main sequential steps."""
    resolved = []
    seq_index = 0
    for step in raw.get("steps", []):
        s = _base_step(step, str_context)
        if step.get("background", False):
            s["background"] = True
        else:
            s["background"] = False
            s["sequence_order"] = seq_index
            seq_index += 1
        resolved.append(s)
    return resolved


def _resolve_loop(raw, str_context, runner, _depth):
    """loop -- repeat steps until exit_condition is met or max_iterations reached."""
    max_iterations = raw.get("max_iterations", 10)
    exit_condition = raw.get("exit_condition", "")
    resolved = []
    for step in raw.get("steps", []):
        s = _base_step(step, str_context)
        s["loop_group"] = True
        s["max_iterations"] = max_iterations
        s["exit_condition"] = exit_condition
        resolved.append(s)
    return resolved


def _resolve_conditional(raw, str_context, runner, _depth):
    """conditional -- branch on a condition; include matching branch (or both if unevaluable)."""
    condition = raw.get("condition", "")
    if_steps = raw.get("if_steps", [])
    else_steps = raw.get("else_steps", [])
    evaluated = _try_evaluate_condition(condition, str_context)
    resolved = []
    if evaluated is True:
        for step in if_steps:
            s = _base_step(step, str_context)
            s["branch"] = "if"
            s["condition"] = condition
            resolved.append(s)
    elif evaluated is False:
        for step in else_steps:
            s = _base_step(step, str_context)
            s["branch"] = "else"
            s["condition"] = condition
            resolved.append(s)
    else:
        for step in if_steps:
            s = _base_step(step, str_context)
            s["branch"] = "if"
            s["condition"] = condition
            s["branch_unevaluated"] = True
            resolved.append(s)
        for step in else_steps:
            s = _base_step(step, str_context)
            s["branch"] = "else"
            s["condition"] = condition
            s["branch_unevaluated"] = True
            resolved.append(s)
    return resolved


def _resolve_delegate(raw, str_context, runner, _depth):
    """delegate -- resolve another named workflow and return its plan (recursive)."""
    steps = raw.get("steps", [])
    resolved = []
    for step in steps:
        target_workflow = step.get("workflow")
        if not target_workflow:
            resolved.append(_base_step(step, str_context))
            continue
        sub_plan = runner.resolve(target_workflow, str_context, _depth=_depth + 1)
        for sub_step in sub_plan:
            sub_step["delegated_from"] = raw.get("name", "unknown")
            sub_step["delegate_step_id"] = step["id"]
            resolved.append(sub_step)
    return resolved


_PATTERN_RESOLVERS = {
    "sequence": _resolve_sequence,
    "parallel": _resolve_parallel,
    "compete": _resolve_compete,
    "escalation": _resolve_escalation,
    "supervisor": _resolve_supervisor,
    "alongside": _resolve_alongside,
    "loop": _resolve_loop,
    "conditional": _resolve_conditional,
    "delegate": _resolve_delegate,
}


# ------------------------------------------------------------------
# Schema validation helpers
# ------------------------------------------------------------------


def _validate_schema(raw: dict, name: str) -> list[str]:
    """Return a list of schema validation error strings (empty = valid)."""
    errors: list[str] = []

    if not isinstance(raw, dict):
        return ["Top-level structure must be a YAML mapping."]

    pattern = raw.get("pattern")
    is_conditional = pattern == "conditional"

    required_fields = ["name", "description", "pattern"]
    if not is_conditional:
        required_fields.append("steps")
    for field in required_fields:
        if field not in raw:
            errors.append(f"Missing required field: '{field}'")

    if "pattern" in raw and raw["pattern"] not in _SUPPORTED_PATTERNS:
        errors.append(
            f"Unsupported pattern '{raw['pattern']}'. "
            f"Allowed: {sorted(_SUPPORTED_PATTERNS)}"
        )

    if pattern in _PATTERN_VALIDATORS:
        _PATTERN_VALIDATORS[pattern](raw, errors)

    if not is_conditional:
        steps = raw.get("steps")
        if steps is not None:
            if not isinstance(steps, list):
                errors.append("'steps' must be a list.")
            elif len(steps) == 0:
                errors.append("'steps' must not be empty.")
            else:
                for i, step in enumerate(steps):
                    errors.extend(_validate_step(step, i, pattern))

    inputs = raw.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        errors.append("'inputs' must be a YAML mapping.")

    outputs = raw.get("outputs")
    if outputs is not None and not isinstance(outputs, dict):
        errors.append("'outputs' must be a YAML mapping.")

    return errors


def _validate_step(step: Any, index: int, pattern: str | None = None) -> list[str]:
    errors: list[str] = []
    prefix = f"steps[{index}]"
    if not isinstance(step, dict):
        return [f"{prefix}: must be a mapping."]
    if pattern == "delegate":
        for field in ("id", "workflow"):
            if field not in step:
                errors.append(f"{prefix}: missing required field '{field}'")
    else:
        for field in ("id", "agent", "prompt_template"):
            if field not in step:
                errors.append(f"{prefix}: missing required field '{field}'")
    if "expects" in step and not isinstance(step["expects"], list):
        errors.append(f"{prefix}.expects: must be a list")
    if "timeout_minutes" in step:
        tm = step["timeout_minutes"]
        if not isinstance(tm, (int, float)) or tm <= 0:
            errors.append(f"{prefix}.timeout_minutes: must be a positive number")
    return errors


def _validate_compete(raw: dict, errors: list[str]) -> None:
    ws = raw.get("winner_strategy")
    if ws is not None and ws not in ("first_pass", "best_score"):
        errors.append(
            f"compete.winner_strategy must be 'first_pass' or 'best_score', got '{ws}'"
        )


def _validate_escalation(raw: dict, errors: list[str]) -> None:
    steps = raw.get("steps") or []
    if isinstance(steps, list):
        for i, step in enumerate(steps):
            if isinstance(step, dict) and "escalation_level" not in step:
                errors.append(f"escalation steps[{i}]: missing required field 'escalation_level'")


def _validate_supervisor(raw: dict, errors: list[str]) -> None:
    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        return
    sup = sum(1 for s in steps if isinstance(s, dict) and s.get("role") == "supervisor")
    wrk = sum(1 for s in steps if isinstance(s, dict) and s.get("role") == "worker")
    if sup == 0:
        errors.append("supervisor pattern: at least one step must have role: supervisor")
    if wrk == 0:
        errors.append("supervisor pattern: at least one step must have role: worker")


def _validate_alongside(raw: dict, errors: list[str]) -> None:
    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        return
    bg = sum(1 for s in steps if isinstance(s, dict) and s.get("background") is True)
    if bg == 0:
        errors.append("alongside pattern: at least one step must have background: true")


def _validate_loop(raw: dict, errors: list[str]) -> None:
    if "max_iterations" not in raw:
        errors.append("loop pattern: missing required field 'max_iterations'")
    else:
        mi = raw["max_iterations"]
        if not isinstance(mi, int) or mi <= 0:
            errors.append("loop pattern: 'max_iterations' must be a positive integer")
    if "exit_condition" not in raw:
        errors.append("loop pattern: missing required field 'exit_condition'")
    elif not isinstance(raw["exit_condition"], str) or not raw["exit_condition"].strip():
        errors.append("loop pattern: 'exit_condition' must be a non-empty string")


def _validate_conditional(raw: dict, errors: list[str]) -> None:
    if "condition" not in raw:
        errors.append("conditional pattern: missing required field 'condition'")
    if "if_steps" not in raw:
        errors.append("conditional pattern: missing required field 'if_steps'")
    else:
        if_steps = raw["if_steps"]
        if not isinstance(if_steps, list) or len(if_steps) == 0:
            errors.append("conditional pattern: 'if_steps' must be a non-empty list")
        else:
            for i, step in enumerate(if_steps):
                for err in _validate_step(step, i):
                    errors.append(f"if_steps[{i}]: {err}")
    if "else_steps" in raw:
        else_steps = raw["else_steps"]
        if not isinstance(else_steps, list):
            errors.append("conditional pattern: 'else_steps' must be a list")
        else:
            for i, step in enumerate(else_steps):
                for err in _validate_step(step, i):
                    errors.append(f"else_steps[{i}]: {err}")


def _validate_delegate(raw: dict, errors: list[str]) -> None:
    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        return
    for i, step in enumerate(steps):
        if isinstance(step, dict) and "workflow" not in step:
            errors.append(f"delegate steps[{i}]: missing required field 'workflow'")


_PATTERN_VALIDATORS: dict[str, Any] = {
    "compete": _validate_compete,
    "escalation": _validate_escalation,
    "supervisor": _validate_supervisor,
    "alongside": _validate_alongside,
    "loop": _validate_loop,
    "conditional": _validate_conditional,
    "delegate": _validate_delegate,
}


def _try_evaluate_condition(condition: str, context: dict[str, str]):
    """Try to evaluate a simple condition. Returns True/False or None if unevaluable."""
    condition = condition.strip()
    m = re.match(r"^(\w+)\s*==\s*(.+)$", condition)
    if m:
        key, expected = m.group(1), m.group(2).strip().strip("'\"")
        if key in context:
            return context[key] == expected
        return None
    m = re.match(r"^(\w+)\s*!=\s*(.+)$", condition)
    if m:
        key, expected = m.group(1), m.group(2).strip().strip("'\"")
        if key in context:
            return context[key] != expected
        return None
    m = re.match(r"^(\w+)$", condition)
    if m:
        key = m.group(1)
        if key in context:
            val = context[key].lower()
            return val not in ("", "false", "0", "no", "none", "null")
        return None
    return None


def _check_required_inputs(raw: dict, context: dict[str, str]) -> list[str]:
    """Return names of required inputs that are absent from context."""
    missing = []
    inputs_spec = raw.get("inputs", {}) or {}
    for input_name, spec in inputs_spec.items():
        if isinstance(spec, dict) and spec.get("required", False):
            if input_name not in context:
                missing.append(input_name)
    return missing


def _interpolate(template: str, context: dict[str, str]) -> str:
    """Replace {{variable}} tokens from context. Step refs and unknown tokens left as-is."""
    def replacer(m: re.Match) -> str:
        token = m.group(1).strip()
        if token.startswith("steps."):
            return m.group(0)
        if token in context:
            return context[token]
        return m.group(0)

    return _TOKEN_RE.sub(replacer, template)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workflow_runner",
        description="YAML workflow runner for the autonomous team.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available workflow names.")

    v = sub.add_parser("validate", help="Validate a workflow file.")
    v.add_argument("workflow", help="Workflow name (without .yaml extension)")

    r = sub.add_parser("resolve", help="Resolve a workflow against provided inputs.")
    r.add_argument("workflow", help="Workflow name (without .yaml extension)")
    r.add_argument(
        "--input",
        action="append",
        metavar="KEY=VALUE",
        dest="inputs",
        default=[],
        help="Input value in KEY=VALUE format. Repeat for multiple inputs.",
    )

    return p


def _parse_inputs(raw_inputs: list[str]) -> dict[str, str]:
    """Parse a list of KEY=VALUE strings into a dict."""
    result: dict[str, str] = {}
    for item in raw_inputs:
        if "=" not in item:
            print(f"invalid --input format (expected KEY=VALUE): {item!r}", file=sys.stderr)
            sys.exit(1)
        key, _, value = item.partition("=")
        result[key.strip()] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    runner = WorkflowRunner()

    if args.command == "list":
        workflows = runner.list_workflows()
        if not workflows:
            print("(no workflows found)")
            return 0
        for name in workflows:
            print(name)
        return 0

    if args.command == "validate":
        try:
            errors = runner.validate(args.workflow)
        except WorkflowNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if errors:
            for err in errors:
                print(f"error: {err}", file=sys.stderr)
            return 1
        print("valid")
        return 0

    if args.command == "resolve":
        context = _parse_inputs(args.inputs)
        try:
            plan = runner.resolve(args.workflow, context)
        except WorkflowNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except MissingInputError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except DelegateDepthError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(plan, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
