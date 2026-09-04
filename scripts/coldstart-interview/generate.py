#!/usr/bin/env python3
"""Deterministic generator: coldstart-interview answers.json -> config.json + CLAUDE.project.md

Pure function of its inputs. No network calls, no GH calls, no subprocess calls.
Re-running against the same answers.json + questions.json always produces
byte-identical output (Spec item 5).

Usage:
    python3 generate.py --answers <path/to/answers.json> --out <output-dir> \
        [--manifest <path/to/questions.json>]

Writes exactly two files into --out:
    config.json        -- dial defaults derived from the "autonomy" topic
    CLAUDE.project.md  -- a project-specific CLAUDE.md overlay (NOT the canonical file)

Never writes to any engine-sync [include] path (scripts/*.sh, hooks/*.py,
.claude/agents/*.md) or to the canonical CLAUDE.md body -- see
test_generate.py::test_engine_boundary_guard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CORE_TOPIC_IDS = ("mode", "identity", "stack", "deploy", "autonomy", "mission")

# Static ceiling table mirroring backend/control_plane.py's dial definitions.
# Hardcoded (not shelled out to control_plane.py) so generation stays a pure
# function of answers.json + questions.json alone -- no dependency on a
# mutable runtime config file.
DIAL_CEILINGS = {
    "agent.spawn": 5,
    "archive.move": 5,
    "cost.spend": 5,
    "deps.bump": 5,
    "docs.write": 5,
    "external.system": 2,
    "intent.generate": 5,
    "memory.write": 5,
    "merge.fast-path": 5,
    "merge.standard": 5,
    "methodology.change": 2,
    "sandbox.modify": 1,
    "tests.add": 5,
}

# Maps an "autonomy" topic question id -> a dial name in DIAL_CEILINGS.
# Only questions with an explicit mapping become config.json dial entries;
# everything else in the autonomy topic (e.g. yes/no gate questions) is
# rendered into CLAUDE.project.md as prose instead.
AUTONOMY_QUESTION_TO_DIAL = {
    "agent_spawn_level": "agent.spawn",
    "merge_fast_path_level": "merge.fast-path",
    "external_system_level": "external.system",
    "intent_generate_level": "intent.generate",
}

DEFAULT_MANIFEST = Path(__file__).parent / "questions.json"

# Sibling data table driving active_roles derivation (Spec item 2, D#1622 Batch
# C1 -- data, not hardcoded generator if/else branches). Edit roles-map.json to
# change role gating, not this file.
DEFAULT_ROLES_MAP = Path(__file__).parent / "roles-map.json"

# The honest empty sentinel used as the manifest default for every "mission"
# topic field (questions.json). Never a fabricated plausible-sounding
# mission -- see build_claude_overlay()'s no-fabrication gate.
MISSION_PLACEHOLDER = "(not captured)"


def _has_real_mission_content(value: object) -> bool:
    """True only when `value` is real operator-supplied content -- i.e. not
    None, not empty/whitespace, and not the manifest's placeholder sentinel.
    Used to gate the Mission / Decision Constitution sections so an
    unanswered or still-default mission topic never renders fabricated
    content (Spec item 6)."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.casefold() == MISSION_PLACEHOLDER.casefold():
        return False
    return True


def load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_roles_map(roles_map_path: Path) -> dict:
    with open(roles_map_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_answers(answers_path: Path) -> dict:
    with open(answers_path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_answers(manifest: dict, answers: dict) -> dict:
    """Merge the manifest's per-question defaults with whatever the answers
    file actually supplied. Missing topics, missing questions, and a
    completely empty/partial answers file are all handled the same way:
    fall back to the manifest default. This is what makes the generator
    tolerant of an abandoned interview (Spec item 10).

    Returns: { topic_id: { question_id: value } } covering every topic and
    question declared in the manifest (core AND optional), so downstream
    code can look up any answer without KeyErrors.
    """
    resolved: dict = {}
    answer_topics = answers.get("topics", {}) if isinstance(answers, dict) else {}
    for topic in manifest.get("topics", []):
        topic_id = topic["id"]
        answered_topic = answer_topics.get(topic_id, {}) or {}
        resolved_topic = {}
        for q in topic.get("questions", []):
            qid = q["id"]
            if qid in answered_topic and answered_topic[qid] not in (None, ""):
                resolved_topic[qid] = answered_topic[qid]
            else:
                resolved_topic[qid] = q.get("default")
        resolved[topic_id] = resolved_topic
    return resolved


def build_config(resolved: dict) -> dict:
    """Build the config.json dial-defaults payload. Only the four autonomy
    questions with an explicit dial mapping become dial entries; dial keys
    are always a subset of DIAL_CEILINGS (which mirrors control_plane.py's
    dial names -- Spec item 6)."""
    autonomy = resolved.get("autonomy", {})
    dials = {}
    for qid, dial_name in AUTONOMY_QUESTION_TO_DIAL.items():
        if qid not in autonomy:
            continue
        level = autonomy[qid]
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        ceiling = DIAL_CEILINGS.get(dial_name, 5)
        level = max(1, min(level, ceiling))
        dials[dial_name] = {"level": level, "ceiling": ceiling, "directives": []}

    identity = resolved.get("identity", {})
    stack = resolved.get("stack", {})
    deploy = resolved.get("deploy", {})
    mode = resolved.get("mode", {})

    return {
        "dials": dials,
        "project": {
            "name": identity.get("project_name"),
            "description": identity.get("project_description"),
            "repo_owner": identity.get("repo_owner"),
            "primary_domain": identity.get("primary_domain"),
            "primary_language": stack.get("primary_language"),
            "deploy_target": deploy.get("deploy_target"),
        },
        "project_mode": mode.get("project_kind", "existing"),
        "gates": {
            "human_approval_before_merge": str(autonomy.get("human_approval_before_merge", "no")).lower() == "yes",
            "security_review": str(autonomy.get("security_review_required", "yes")).lower() == "yes",
        },
    }


def build_active_roles(resolved: dict, roles_map: dict) -> list:
    """Derive the sorted active_roles allowlist from resolved answers and the
    roles-map.json data table (Spec items 1-4, 6). Always-on core roles are
    unconditional; UI-only roles gate on stack.has_frontend; release/runbook/
    incident roles gate on deploy.deploy_target being a real environment
    (not none/local/n/a/blank)."""
    stack = resolved.get("stack", {})
    deploy = resolved.get("deploy", {})

    roles = set(roles_map.get("always_on", []))

    if str(stack.get("has_frontend", "no")).strip().lower() == "yes":
        roles |= set(roles_map.get("frontend_gated", []))

    non_real_targets = {str(v).strip().lower() for v in roles_map.get("non_real_deploy_targets", [])}
    deploy_target = str(deploy.get("deploy_target", "")).strip().lower()
    if deploy_target not in non_real_targets:
        roles |= set(roles_map.get("deploy_gated", []))

    return sorted(roles)


def build_claude_overlay(resolved: dict) -> str:
    identity = resolved.get("identity", {})
    stack = resolved.get("stack", {})
    deploy = resolved.get("deploy", {})
    autonomy = resolved.get("autonomy", {})
    mission = resolved.get("mission", {})

    product_vision = mission.get("product_vision")
    why_now = mission.get("why_now")
    guiding_principles = mission.get("guiding_principles")

    lines = []
    lines.append(f"# {identity.get('project_name', 'Project')} — Project Overlay")
    lines.append("")
    lines.append(
        "This file is generated by `scripts/coldstart-interview/generate.py` from "
        "`answers.json`. It is a project-specific overlay, NOT part of the engine-synced "
        "canonical CLAUDE.md. Re-run the generator after editing `answers.json` to "
        "regenerate this file rather than hand-editing it."
    )
    lines.append("")
    lines.append(
        "**Pointer line** — add this line near the top of the canonical `CLAUDE.md` "
        "(this generator does not do so automatically; wiring is a Slice C follow-on):"
    )
    lines.append("")
    lines.append("> See `CLAUDE.project.md` for project-specific overlay and customization.")
    lines.append("")
    lines.append("## Project Identity")
    lines.append("")
    lines.append(f"- **Name:** {identity.get('project_name')}")
    lines.append(f"- **Description:** {identity.get('project_description')}")
    lines.append(f"- **Repo owner:** {identity.get('repo_owner')}")
    lines.append(f"- **Domain:** {identity.get('primary_domain')}")
    lines.append(f"- **Target users:** {identity.get('target_users')}")
    lines.append("")

    # No-fabrication gate (Spec items 5-6): only emit "## Mission" when the
    # operator supplied real product_vision or why_now content -- never a
    # placeholder-derived fake mission.
    if _has_real_mission_content(product_vision) or _has_real_mission_content(why_now):
        lines.append("## Mission")
        lines.append("")
        if _has_real_mission_content(product_vision):
            lines.append(str(product_vision))
            lines.append("")
        if _has_real_mission_content(why_now):
            lines.append(f"**Why now:** {why_now}")
            lines.append("")

    lines.append("## Tech Stack")
    lines.append("")
    lines.append(f"- **Primary language:** {stack.get('primary_language')}")
    lines.append(f"- **Framework/runtime:** {stack.get('runtime_framework')}")
    lines.append(f"- **Package manager:** {stack.get('package_manager')}")
    lines.append(f"- **Test framework:** {stack.get('test_framework')}")
    lines.append(f"- **Has frontend:** {stack.get('has_frontend')}")
    lines.append("")
    lines.append("## Deploy Target")
    lines.append("")
    lines.append(f"- **Deploy target:** {deploy.get('deploy_target')}")
    lines.append(f"- **CI provider:** {deploy.get('ci_provider')}")
    lines.append(f"- **Staging environment:** {deploy.get('has_staging_env')}")
    lines.append(f"- **Release cadence:** {deploy.get('release_cadence')}")
    if deploy.get("hosting_provider") not in (None, "n/a"):
        lines.append(f"- **Hosting provider:** {deploy.get('hosting_provider')}")
    if deploy.get("domain") not in (None, "n/a"):
        lines.append(f"- **Domain:** {deploy.get('domain')}")
    lines.append("")
    lines.append("## Autonomy Dials")
    lines.append("")
    lines.append(
        "Dial defaults derived from these answers are written to `config.json` "
        "under the `dials` key (agent.spawn, merge.fast-path, external.system, "
        "intent.generate)."
    )
    lines.append(f"- **Human approval required before merge:** {autonomy.get('human_approval_before_merge')}")
    lines.append(f"- **Security review required on every PR:** {autonomy.get('security_review_required')}")
    lines.append("")

    # No-fabrication gate (Spec items 5-6): only emit "## Decision
    # Constitution" when the operator supplied real guiding_principles.
    if _has_real_mission_content(guiding_principles):
        lines.append("## Decision Constitution")
        lines.append("")
        lines.append(str(guiding_principles))
        lines.append("")

    lines.append(
        "_Role-roster trimming is applied via config.json's `active_roles` "
        "field. Initial backlog seeding and provenance-marker coordination "
        "with the engine-sync three-way merge remain Slice C follow-ons and "
        "are not applied by this generator._"
    )
    lines.append("")
    return "\n".join(lines)


def generate(
    answers_path: Path,
    out_dir: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    roles_map_path: Path = DEFAULT_ROLES_MAP,
) -> None:
    manifest = load_manifest(manifest_path)
    answers = load_answers(answers_path)
    resolved = resolve_answers(manifest, answers)
    roles_map = load_roles_map(roles_map_path)

    config = build_config(resolved)
    config["active_roles"] = build_active_roles(resolved, roles_map)
    overlay = build_claude_overlay(resolved)

    out_dir.mkdir(parents=True, exist_ok=True)

    config_path = out_dir / "config.json"
    overlay_path = out_dir / "CLAUDE.project.md"

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)
        f.write("\n")

    with open(overlay_path, "w", encoding="utf-8") as f:
        f.write(overlay)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", required=True, type=Path, help="Path to answers.json")
    parser.add_argument("--out", required=True, type=Path, help="Output directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to questions.json (defaults to the manifest next to this script)",
    )
    parser.add_argument(
        "--roles-map",
        type=Path,
        default=DEFAULT_ROLES_MAP,
        help="Path to roles-map.json (defaults to the map next to this script)",
    )
    args = parser.parse_args(argv)

    generate(args.answers, args.out, args.manifest, args.roles_map)
    return 0


if __name__ == "__main__":
    sys.exit(main())
