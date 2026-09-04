#!/usr/bin/env python3
"""seed-backlog.py — turn coldstart-interview answers into a starter GH Discussion backlog.

A SEPARATE script from generate.py (which stays pure/deterministic/network-free).
Two phases:

  PLAN (offline, deterministic):
    python3 seed-backlog.py --plan-only --answers <path/to/answers.json>
    Prints a JSON array of seed specs ({"title", "body", "category"}), 3-7 of
    them, tailored to the answers' stack/deploy/domain. No network call.

  APPLY (networked):
    python3 seed-backlog.py --answers <path/to/answers.json> [--max-retries N]
    Sends the planned seeds to GitHub via `gh api graphql` createDiscussion,
    resolving owner/repo from the answers themselves (identity.repo_owner /
    identity.project_name) -- never a hardcoded or foreign repo. Retries with
    exponential backoff on a GH 5xx ONLY; a 4xx fails immediately, no retry.

On persistent failure (retries exhausted, or --offline) the unsent seeds are
written to a replay file and the script exits 0 -- coldstart must never be
aborted by a seeding failure.

  REPLAY:
    python3 seed-backlog.py --replay <path/to/replay-file.json>
    Re-sends saved specs, skipping any seed whose title already exists as an
    open Discussion in the target repo (idempotent).

  SELF-TEST (offline, no network, used by CI / --self-test callers):
    python3 seed-backlog.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from generate import (  # noqa: E402
    DEFAULT_MANIFEST,
    _has_real_mission_content,
    load_answers,
    load_manifest,
    resolve_answers,
)

DEFAULT_CATEGORY = "Ideas"
MIN_SEEDS = 3
MAX_SEEDS = 7
DEFAULT_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# PLAN phase — pure, deterministic, no network, no subprocess.
# ---------------------------------------------------------------------------

def derive_repo_target(resolved: dict) -> tuple[str, str]:
    """Resolve (owner, name) for GitHub calls STRICTLY from the coldstarted
    project's own answers -- never a hardcoded or foreign repo."""
    identity = resolved.get("identity", {}) or {}
    owner = identity.get("repo_owner")
    name = identity.get("project_name")
    if not owner or not name:
        raise ValueError(
            "answers.json is missing identity.repo_owner or identity.project_name -- "
            "cannot resolve the target repo"
        )
    return str(owner), str(name)


def _clean(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text or text.lower() in ("none", "n/a"):
        return fallback
    return text


def plan_seeds(resolved: dict) -> list[dict]:
    """Deterministic PLAN: build a bounded (3-7), never-empty, never-spammy
    set of seed Discussion specs tailored to the answers' stack/deploy/domain.
    Human Voice Standard -- no "the Spec" / internal-process references, these
    seeds are meant for a real new project's backlog."""
    identity = resolved.get("identity", {}) or {}
    stack = resolved.get("stack", {}) or {}
    deploy = resolved.get("deploy", {}) or {}
    mission = resolved.get("mission", {}) or {}

    project_name = _clean(identity.get("project_name"), "this project")
    domain = _clean(identity.get("primary_domain"), "the core product")
    language = _clean(stack.get("primary_language"), "the chosen language")
    framework = stack.get("runtime_framework")
    ci_provider = _clean(deploy.get("ci_provider"), "a CI pipeline")
    deploy_target = deploy.get("deploy_target")
    has_frontend = str(stack.get("has_frontend", "no")).strip().lower() == "yes"
    has_staging = str(deploy.get("has_staging_env", "no")).strip().lower() == "yes"
    why_now = mission.get("why_now")

    stack_desc = language
    if framework and str(framework).strip().lower() not in ("none", "n/a", ""):
        stack_desc = f"{language}/{framework}"

    seeds: list[dict] = []

    seeds.append({
        "title": f"Set up CI for {stack_desc}",
        "body": (
            f"Get {ci_provider} running lint and tests for {project_name} on every push. "
            f"Start with the existing local build/test commands and wire them into CI "
            f"as-is before adding anything fancier."
        ),
        "category": DEFAULT_CATEGORY,
    })

    if deploy_target and str(deploy_target).strip().lower() not in ("none", "n/a", ""):
        seeds.append({
            "title": f"Configure deploy to {deploy_target}",
            "body": (
                f"Wire up the deploy pipeline for {project_name} targeting {deploy_target}. "
                f"A manual first deploy is fine -- automate it once the steps are known."
            ),
            "category": DEFAULT_CATEGORY,
        })

    seeds.append({
        "title": "Write the onboarding README",
        "body": (
            f"Document how a new contributor gets {project_name} running locally: "
            f"prerequisites, install steps, how to run the tests, how to run the app."
        ),
        "category": DEFAULT_CATEGORY,
    })

    seeds.append({
        "title": f"First feature spike: {domain}",
        "body": (
            f"Pick the smallest useful slice of {domain} functionality and ship it "
            f"end to end -- the goal is a thin vertical slice, not full coverage."
        ),
        "category": DEFAULT_CATEGORY,
    })

    if has_frontend:
        seeds.append({
            "title": f"Wire up the frontend shell ({framework or 'chosen framework'})",
            "body": (
                f"Stand up the base layout, routing, and a placeholder home screen for "
                f"{project_name} so feature work has somewhere to land."
            ),
            "category": DEFAULT_CATEGORY,
        })

    if has_staging:
        seeds.append({
            "title": "Stand up a staging environment",
            "body": (
                f"Mirror the {deploy_target or 'production'} deploy target in a staging "
                f"environment so changes can be verified before they ship."
            ),
            "category": DEFAULT_CATEGORY,
        })

    if _has_real_mission_content(why_now):
        why_now_text = str(why_now).strip()
        excerpt = why_now_text if len(why_now_text) <= 80 else why_now_text[:77] + "..."
        seeds.append({
            "title": f"Tackle the core problem: {excerpt}",
            "body": (
                f"This is the reason {project_name} exists. Turn it into a concrete "
                f"first milestone rather than letting it stay an abstract goal."
            ),
            "category": DEFAULT_CATEGORY,
        })

    # Bounded, never zero, never spammy.
    if len(seeds) < MIN_SEEDS:
        seeds.append({
            "title": "Set up basic test coverage",
            "body": f"Add a first pass of automated tests for {project_name}'s core path.",
            "category": DEFAULT_CATEGORY,
        })
    seeds = seeds[:MAX_SEEDS]
    return seeds


# ---------------------------------------------------------------------------
# APPLY phase — networked, retried, injectable transport.
# ---------------------------------------------------------------------------

@dataclass
class TransportResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class GHCliTransport:
    """Real transport -- shells out to `gh api graphql`. Injected so the
    retry/backoff/idempotency logic is unit-testable offline with a stub."""

    def run_graphql(self, args: list[str], timeout: int = 30) -> TransportResult:
        try:
            proc = subprocess.run(
                ["gh", "api", "graphql", *args],
                capture_output=True, text=True, timeout=timeout,
            )
            return TransportResult(proc.returncode, proc.stdout, proc.stderr)
        except Exception as exc:  # pragma: no cover - defensive
            return TransportResult(returncode=1, stdout="", stderr=str(exc))


_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def extract_http_status(stderr: str) -> Optional[int]:
    m = _HTTP_STATUS_RE.search(stderr or "")
    return int(m.group(1)) if m else None


def run_graphql_with_retry(
    transport: Any,
    args: list[str],
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TransportResult:
    """Run one GraphQL call, retrying with exponential backoff ONLY when the
    failure is an extractable 5xx. A 4xx (or unclassified) failure returns
    immediately with no retry (Spec item 4)."""
    attempt = 0
    result = transport.run_graphql(args)
    while result.returncode != 0:
        status = extract_http_status(result.stderr)
        if status is not None and 500 <= status < 600 and attempt < max_retries:
            sleep_fn(2 ** attempt)
            attempt += 1
            result = transport.run_graphql(args)
            continue
        break
    return result


def get_repo_id(transport, owner, name, max_retries=DEFAULT_MAX_RETRIES, sleep_fn=time.sleep) -> str:
    query = f'query{{repository(owner:"{owner}",name:"{name}"){{id}}}}'
    result = run_graphql_with_retry(transport, ["-f", f"query={query}"], max_retries, sleep_fn)
    if result.returncode != 0:
        raise RuntimeError(f"repo id lookup failed for {owner}/{name}: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    return data["data"]["repository"]["id"]


def get_category_id(
    transport, owner, name, category_name=DEFAULT_CATEGORY,
    max_retries=DEFAULT_MAX_RETRIES, sleep_fn=time.sleep,
) -> str:
    query = (
        f'query{{repository(owner:"{owner}",name:"{name}")'
        f'{{discussionCategories(first:20){{nodes{{id name}}}}}}}}'
    )
    result = run_graphql_with_retry(transport, ["-f", f"query={query}"], max_retries, sleep_fn)
    if result.returncode != 0:
        raise RuntimeError(f"category lookup failed for {owner}/{name}: {result.stderr[:200]}")
    data = json.loads(result.stdout)
    nodes = data["data"]["repository"]["discussionCategories"]["nodes"]
    by_name = {n["name"]: n["id"] for n in nodes}
    if category_name in by_name:
        return by_name[category_name]
    if DEFAULT_CATEGORY in by_name:
        return by_name[DEFAULT_CATEGORY]
    if nodes:
        return nodes[0]["id"]
    raise RuntimeError(f"no discussion categories found for {owner}/{name}")


def list_open_discussion_titles(
    transport, owner, name, max_retries=DEFAULT_MAX_RETRIES, sleep_fn=time.sleep,
) -> set[str]:
    query = (
        f'query{{repository(owner:"{owner}",name:"{name}")'
        f'{{discussions(first:100,states:OPEN){{nodes{{title}}}}}}}}'
    )
    result = run_graphql_with_retry(transport, ["-f", f"query={query}"], max_retries, sleep_fn)
    if result.returncode != 0:
        # Fail closed on the query itself -- treat as "unknown", not "none
        # exist". Caller keeps going but idempotency can't be verified.
        return set()
    data = json.loads(result.stdout)
    nodes = data["data"]["repository"]["discussions"]["nodes"]
    return {n["title"] for n in nodes}


def create_discussion(
    transport, repo_id, category_id, title, body,
    max_retries=DEFAULT_MAX_RETRIES, sleep_fn=time.sleep,
) -> dict:
    mutation = (
        "mutation CreateDiscussion($repoId:ID!,$catId:ID!,$title:String!,$body:String!){"
        "createDiscussion(input:{repositoryId:$repoId,categoryId:$catId,"
        "title:$title,body:$body}){discussion{url number}}}"
    )
    args = [
        "-f", f"query={mutation}",
        "-f", f"repoId={repo_id}",
        "-f", f"catId={category_id}",
        "-f", f"title={title}",
        "-f", f"body={body}",
    ]
    result = run_graphql_with_retry(transport, args, max_retries, sleep_fn)
    if result.returncode != 0:
        raise RuntimeError(f"createDiscussion failed for {title!r}: {result.stderr[:300]}")
    data = json.loads(result.stdout)
    return data["data"]["createDiscussion"]["discussion"]


def apply_seeds(
    seeds: list[dict], owner: str, name: str, transport: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    skip_titles: Optional[set[str]] = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Send `seeds` to (owner, name). Returns (sent, unsent, errors). Never
    raises for a single seed's failure -- collects it into `unsent` and keeps
    going. Only raises (via caller catching) if repo/category resolution
    itself fails entirely."""
    skip_titles = skip_titles or set()
    sent: list[dict] = []
    unsent: list[dict] = []
    errors: list[str] = []

    repo_id = get_repo_id(transport, owner, name, max_retries, sleep_fn)
    category_ids: dict[str, str] = {}

    for seed in seeds:
        if seed["title"] in skip_titles:
            continue
        cat_name = seed.get("category", DEFAULT_CATEGORY)
        try:
            if cat_name not in category_ids:
                category_ids[cat_name] = get_category_id(
                    transport, owner, name, cat_name, max_retries, sleep_fn
                )
            disc = create_discussion(
                transport, repo_id, category_ids[cat_name],
                seed["title"], seed["body"], max_retries, sleep_fn,
            )
            sent.append({"seed": seed, "discussion": disc})
        except Exception as exc:
            unsent.append(seed)
            errors.append(str(exc))

    return sent, unsent, errors


# ---------------------------------------------------------------------------
# Replay file — degrade-and-never-abort.
# ---------------------------------------------------------------------------

def replay_file_path(session: Optional[str] = None, base_dir: Optional[str] = None) -> Path:
    state_dir = base_dir or os.environ.get("AUTONOMOUS_TEAM_STATE_DIR") or ".autonomous-team"
    session_id = session or "no-session"
    return Path(state_dir) / "coldstart" / session_id / "backlog-replay.json"


def write_replay(path: Path, owner: str, name: str, seeds: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo_owner": owner, "repo_name": name, "seeds": seeds}
    path.write_text(json.dumps(payload, indent=2))


def load_replay(path: str) -> tuple[str, str, list[dict]]:
    payload = json.loads(Path(path).read_text())
    return payload["repo_owner"], payload["repo_name"], payload["seeds"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolved_from_answers(answers_path: str, manifest_path: str) -> dict:
    manifest = load_manifest(Path(manifest_path))
    answers = load_answers(Path(answers_path))
    return resolve_answers(manifest, answers)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true", help="Emit the seed plan only, no network call.")
    parser.add_argument("--self-test", action="store_true", help="Exercise the PLAN phase offline against a bundled fixture; no network.")
    parser.add_argument("--answers", help="Path to answers.json")
    parser.add_argument("--replay", help="Path to a replay file written on a prior degraded run")
    parser.add_argument("--offline", action="store_true", help="Skip the network call entirely and write a replay file.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--session", default=None, help="Session id, used for the replay file path")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args(argv)

    if args.self_test:
        fixture = _HERE / "tests" / "fixtures" / "answers-mission.json"
        resolved = _resolved_from_answers(str(fixture), args.manifest)
        seeds = plan_seeds(resolved)
        if not (MIN_SEEDS <= len(seeds) <= MAX_SEEDS):
            print(f"[seed-backlog] self-test FAIL: plan size {len(seeds)} out of bounds", file=sys.stderr)
            return 1
        print(json.dumps(seeds, indent=2))
        print("[seed-backlog] self-test OK: plan-only, zero network calls", file=sys.stderr)
        return 0

    if args.replay:
        owner, name, seeds = load_replay(args.replay)
        transport = GHCliTransport()
        try:
            existing_titles = list_open_discussion_titles(transport, owner, name, args.max_retries)
        except Exception:
            existing_titles = set()
        try:
            sent, unsent, errors = apply_seeds(
                seeds, owner, name, transport, args.max_retries, skip_titles=existing_titles,
            )
        except Exception as exc:
            sent, unsent, errors = [], seeds, [str(exc)]
        skipped = sum(1 for s in seeds if s["title"] in existing_titles)
        if unsent:
            path = replay_file_path(args.session)
            write_replay(path, owner, name, unsent)
            print(f"degraded to replay file: {path}")
        else:
            print(f"[seed-backlog] replay complete: {len(sent)} sent, {skipped} skipped (already open)")
        return 0

    if not args.answers:
        print("error: --answers is required unless --replay or --self-test", file=sys.stderr)
        return 2

    resolved = _resolved_from_answers(args.answers, args.manifest)
    owner, name = derive_repo_target(resolved)
    seeds = plan_seeds(resolved)

    if args.plan_only:
        print(json.dumps(seeds, indent=2))
        return 0

    if args.offline:
        path = replay_file_path(args.session)
        write_replay(path, owner, name, seeds)
        print(f"degraded to replay file: {path}")
        return 0

    transport = GHCliTransport()
    try:
        sent, unsent, errors = apply_seeds(seeds, owner, name, transport, args.max_retries)
    except Exception as exc:
        sent, unsent, errors = [], seeds, [str(exc)]

    if unsent:
        path = replay_file_path(args.session)
        write_replay(path, owner, name, unsent)
        print(f"degraded to replay file: {path}")
        if errors:
            print(f"[seed-backlog] {len(errors)} error(s), e.g.: {errors[0][:200]}", file=sys.stderr)
    else:
        print(f"[seed-backlog] seeded {len(sent)} Discussion(s) in {owner}/{name}")

    return 0  # coldstart is never aborted by a seeding failure


if __name__ == "__main__":
    sys.exit(main())
