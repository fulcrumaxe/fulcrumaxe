#!/usr/bin/env python3
"""scripts/ci/update-apply-guard.py — behavioral CI guard for update-apply.sh
(D#2335 Spec, PR 2, acceptance items 17-22).

Sibling of scripts/ci/update-check-guard.py, and the same kind of thing it
is: NOT a lint over source text. It builds a scratch "adopter project" and a
scratch "engine tree" in a tmpdir and drives the real
scripts/update-apply.sh subprocess against them, asserting the behavior the
Spec requires — that the first apply writes nothing, that the second one
does, that a re-run reports already-up-to-date and writes nothing, that
local .autonomous-team state and $AUTONOMOUS_TEAM_STATE_DIR survive, and
that no refusal path ever prints anything resembling "up to date".

Hermetic: no network, no GH_TOKEN, no new dependency. The upstream read
update-check.sh would make is injected via UPDATE_CHECK_UPSTREAM_CMD (which
propagates naturally, since update-apply.sh runs update-check.sh as a
subprocess), and the engine is a fixture tree this file writes.

Why a fixture engine rather than this repo's own loop-bootstrap/: a real
bootstrap run shells out to `gh` for labels, bot_account and the team-log
Issue, which is neither hermetic nor free. What update-apply.sh actually
contributes is orchestration — verdict routing, engine-root resolution, the
preview gate, marker handling, and honest post-apply reporting — and that is
what a controlled bootstrap lets this file test end to end. The fixture is
written to match what the real bootstrap.sh does on the points these
assertions depend on (skip-if-exists on the .autonomous-team/*.json files,
an engine-install.json stamp with the same five keys, never removing
anything); per the D#2213 rule, it stands in for the real tool by matching
what that tool does, not by matching what these checks want to see. The real
end-to-end path — a real bootstrap against a real coldstarted project — is
covered by the PR's Gate 2 transcript, not by this hermetic guard.

Run from the repo root:

    python3 scripts/ci/update-apply-guard.py

Exit 0: every assertion passed.
Exit 1: at least one assertion failed. Details printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UPDATE_APPLY = REPO_ROOT / "scripts" / "update-apply.sh"
UPDATE_CHECK = REPO_ROOT / "scripts" / "update-check.sh"
REPO_RESOLVE = REPO_ROOT / "scripts" / "lib" / "repo-resolve.sh"

# The stamp SHA the fixture engine's bootstrap records, and the SHA the
# injected upstream command treats as "upstream HEAD". Anything else the
# stamp holds is reported as 3 commits behind.
STALE_SHA = "1111111111111111111111111111111111111111"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(condition: bool, description: str) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {description}")
    else:
        FAIL += 1
        FAILURES.append(description)
        print(f"  FAIL: {description}")


FIXTURE_BOOTSTRAP = r"""#!/usr/bin/env bash
# Fixture stand-in for loop-bootstrap/bootstrap.sh, used only by
# scripts/ci/update-apply-guard.py. It reproduces the behaviors
# update-apply.sh depends on, as the real script performs them:
#   - requires --repo OWNER/NAME and a target that is a git repo
#   - installs files into the target (creating and overwriting)
#   - SKIPS .autonomous-team/config.json, project.json and agent-profiles.json
#     when they already exist (the real script's [[ -f ]] guards)
#   - SKIPS a .claude/agents/*.md file whose local copy differs from the
#     engine's, and reports the skipped set once at the end (the real
#     do_install_agent + AGENT_UPSTREAM_UPDATES pass; the report lines below
#     are copied verbatim from bootstrap.sh:508-517, per the D#2213 rule that
#     a double is validated against what the real tool writes)
#   - writes .autonomous-team/engine-install.json on every run, with the same
#     five keys the real step 19a writes
#   - never removes anything from the target
# It makes no network call and touches no path outside the target.
set -euo pipefail

TARGET=""
TARGET_REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) TARGET_REPO="${2:-}"; shift 2 ;;
    --dry-run|--force) shift ;;
    -*) echo "Unknown flag: $1" >&2; exit 1 ;;
    *) TARGET="$1"; shift ;;
  esac
done
[[ -n "$TARGET" ]] || { echo "ERROR: no target" >&2; exit 1; }
[[ -n "$TARGET_REPO" ]] || { echo "ERROR: --repo required" >&2; exit 1; }
git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1 || { echo "ERROR: $TARGET is not a git repository" >&2; exit 1; }

ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$TARGET/scripts" "$TARGET/backend" "$TARGET/.autonomous-team"
cp "$ENGINE_ROOT/scripts/engine-new-tool.sh" "$TARGET/scripts/engine-new-tool.sh"
cp "$ENGINE_ROOT/backend/engine_payload.py" "$TARGET/backend/engine_payload.py"

for f in config.json project.json agent-profiles.json; do
  if [[ ! -f "$TARGET/.autonomous-team/$f" ]]; then
    echo '{}' > "$TARGET/.autonomous-team/$f"
  fi
done

# Agent definitions: install when absent, skip (and report) when the local
# copy diverges — the real do_install_agent's default, non---force behavior.
mkdir -p "$TARGET/.claude/agents"
AGENT_UPSTREAM_UPDATES=()
for src in "$ENGINE_ROOT"/.claude/agents/*.md; do
  [[ -f "$src" ]] || continue
  dst="$TARGET/.claude/agents/$(basename "$src")"
  if [[ -f "$dst" ]]; then
    cmp -s "$src" "$dst" || AGENT_UPSTREAM_UPDATES+=("$(basename "$src")")
  else
    cp "$src" "$dst"
  fi
done

if [[ "${#AGENT_UPSTREAM_UPDATES[@]}" -gt 0 ]]; then
  echo ""
  echo "${#AGENT_UPSTREAM_UPDATES[@]} agent definition(s) have upstream updates you are not receiving:"
  for a in "${AGENT_UPSTREAM_UPDATES[@]}"; do
    echo "    $a"
  done
  echo "  Review:  diff $TARGET/.claude/agents/<file> $ENGINE_ROOT/.claude/agents/<file>"
  echo "  Accept:  bash loop-bootstrap/bootstrap.sh --force --repo $TARGET_REPO $TARGET"
  echo "           (--force also overwrites any local edits to those files)"
  echo "  CLAUDE.md: never updated by bootstrap after first install — diff"
  echo "             loop-bootstrap/team-lead-protocol.md yourself after upgrades."
fi
echo ""
echo "==> done"

ENGINE_COMMIT_VAL="$(git -C "$ENGINE_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
python3 - "$TARGET/.autonomous-team/engine-install.json" "$ENGINE_COMMIT_VAL" "$TARGET_REPO" <<'STAMP_PY'
import json, sys
from datetime import datetime, timezone
dst, commit, repo = sys.argv[1:4]
with open(dst, "w") as f:
    json.dump({
        "engine_version": "fixture",
        "engine_commit": commit or None,
        "source": "clone",
        "source_repo": repo,
        "bootstrapped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, f, indent=2)
    f.write("\n")
STAMP_PY
echo "fixture bootstrap installed into $TARGET (repo $TARGET_REPO)"
"""


def git(*args: str, cwd: Path) -> None:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "guard",
        "GIT_AUTHOR_EMAIL": "guard@localhost",
        "GIT_COMMITTER_NAME": "guard",
        "GIT_COMMITTER_EMAIL": "guard@localhost",
    })
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_engine(root: Path, payload: str) -> Path:
    """A scratch tree that passes update-apply.sh's engine content check."""
    engine = root / "engine"
    (engine / "loop-bootstrap").mkdir(parents=True)
    (engine / "scripts").mkdir()
    (engine / "backend").mkdir()
    bs = engine / "loop-bootstrap" / "bootstrap.sh"
    bs.write_text(FIXTURE_BOOTSTRAP)
    bs.chmod(bs.stat().st_mode | stat.S_IXUSR)
    (engine / "scripts" / "engine-new-tool.sh").write_text(f"# {payload}\n")
    (engine / "backend" / "engine_payload.py").write_text(f"PAYLOAD = {payload!r}\n")
    (engine / ".claude" / "agents").mkdir(parents=True)
    (engine / ".claude" / "agents" / "example.md").write_text(f"# agent definition {payload}\n")
    git("init", "-q", cwd=engine)
    git("add", "-A", cwd=engine)
    git("commit", "-qm", "fixture engine", cwd=engine)
    return engine


def make_project(root: Path, name: str, *, stamp_sha: str | None = STALE_SHA) -> Path:
    """A scratch tree that looks like a bootstrapped adopter project.

    update-apply.sh and update-check.sh both resolve their repo root as the
    directory two levels above themselves, so real copies of both have to
    live at <project>/scripts/ for that resolution to land here rather than
    on this guard's own checkout.
    """
    project = root / name
    (project / "scripts" / "lib").mkdir(parents=True)
    (project / ".autonomous-team").mkdir()
    # A locally-edited agent definition: bootstrap must skip it and report it,
    # and the preview must relay that report rather than swallow it.
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "agents" / "example.md").write_text("# agent definition LOCALLY EDITED\n")
    for src in (UPDATE_APPLY, UPDATE_CHECK):
        shutil.copy2(src, project / "scripts" / src.name)
    shutil.copy2(REPO_RESOLVE, project / "scripts" / "lib" / "repo-resolve.sh")
    (project / ".autonomous-team" / "config.json").write_text(
        json.dumps({"repo": "acme/widget", "bot_account": "acme-bot", "local_edit": "keep me"},
                   indent=2) + "\n")
    (project / ".autonomous-team" / "project.json").write_text(
        json.dumps({"repo": "acme/widget", "local_edit": "keep me too"}, indent=2) + "\n")
    (project / ".autonomous-team" / "agent-profiles.json").write_text(
        json.dumps({"local_edit": "and me"}, indent=2) + "\n")
    if stamp_sha is not None:
        (project / ".autonomous-team" / "engine-install.json").write_text(
            json.dumps({
                "engine_version": "fixture",
                "engine_commit": stamp_sha,
                "source": "clone",
                "source_repo": "acme/widget",
                "bootstrapped_at": "2026-09-01T00:00:00Z",
            }, indent=2) + "\n")
    git("init", "-q", cwd=project)
    return project


def tree_digest(root: Path, *, exclude: set[str] | None = None) -> str:
    """Recursive content checksum of a tree, .git excluded."""
    exclude = exclude or set()
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel in exclude:
                continue
            h.update(rel.encode())
            try:
                h.update(hashlib.sha256(p.read_bytes()).digest())
            except OSError:
                h.update(b"<unreadable>")
    return h.hexdigest()


MARKER_REL = ".autonomous-team/update-preview.json"


def upstream_cmd(current_sha: str) -> str:
    """Injected stand-in for update-check.sh's one `gh api` compare call.

    Contract (from update-check.sh's header): exit 0 + a bare integer is
    "that many commits ahead". Here, a stamp holding the engine's own commit
    is up to date; anything else is 3 behind.
    """
    return f'if [ "$UPDATE_CHECK_BASELINE_SHA" = "{current_sha}" ]; then echo 0; else echo 3; fi'


def run(project: Path, args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Prove no token is needed anywhere in this file.
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "FULCRUMAXE_ENGINE_ROOT", "AUTONOMOUS_TEAM_REPO"):
        env.pop(var, None)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(project / "scripts" / "update-apply.sh"), *args],
        cwd=project, env=env, capture_output=True, text=True,
    )


def main() -> int:
    if not UPDATE_APPLY.is_file():
        print(f"FAIL: {UPDATE_APPLY} does not exist", file=sys.stderr)
        return 1

    have_rsync = shutil.which("rsync") is not None

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        print("update-apply.sh guard (D#2335 PR 2)")
        print()

        # --- usage surface ---------------------------------------------------
        print("usage surface:")
        project = make_project(tmp_path, "usage")
        result = run(project, ["--help"])
        check(result.returncode == 0, "--help exits 0")
        for code in ("0", "10", "20", "2"):
            check(re.search(rf"^\s+{code}\s", result.stdout, re.M) is not None,
                  f"--help documents exit code {code}")
        result = run(project, ["--bogus-flag"])
        check(result.returncode == 2, "--bogus-flag exits 2")
        check("unknown argument" in result.stderr, "--bogus-flag explains itself on stderr")

        # --- refusals write nothing and never say 'up to date' ---------------
        print()
        print("refusal paths:")
        engine = make_engine(tmp_path, "v2")
        engine_sha = subprocess.run(["git", "-C", str(engine), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True).stdout.strip()
        up = upstream_cmd(engine_sha)

        project = make_project(tmp_path, "no-engine")
        before = tree_digest(project)
        result = run(project, [], {"UPDATE_CHECK_UPSTREAM_CMD": up})
        check(result.returncode == 20, "no --engine-root exits 20")
        check("reason=engine_root_unresolved" in result.stderr,
              "no --engine-root reports reason=engine_root_unresolved")
        check(tree_digest(project) == before, "no --engine-root writes nothing")
        check("up to date" not in result.stderr, "no --engine-root never prints 'up to date'")

        project = make_project(tmp_path, "bad-engine")
        not_engine = tmp_path / "not-an-engine"
        not_engine.mkdir()
        result = run(project, ["--engine-root", str(not_engine)], {"UPDATE_CHECK_UPSTREAM_CMD": up})
        check(result.returncode == 20, "a non-engine --engine-root exits 20")
        check("reason=engine_root_not_engine" in result.stderr,
              "a non-engine --engine-root reports reason=engine_root_not_engine")

        project = make_project(tmp_path, "self-engine")
        result = run(project, ["--engine-root", str(project)], {"UPDATE_CHECK_UPSTREAM_CMD": up})
        check(result.returncode == 20, "--engine-root pointing at this repo exits 20")
        check("reason=engine_root_is_target" in result.stderr,
              "--engine-root pointing at this repo reports reason=engine_root_is_target")

        # --- already up to date: exit 0, nothing written (acceptance 20) -----
        print()
        print("already-up-to-date path (acceptance 20):")
        project = make_project(tmp_path, "current", stamp_sha=engine_sha)
        before = tree_digest(project)
        result = run(project, ["--engine-root", str(engine)], {"UPDATE_CHECK_UPSTREAM_CMD": up})
        check(result.returncode == 0, "an up-to-date install exits 0")
        check("already up to date" in result.stdout, "an up-to-date install says so")
        check(tree_digest(project) == before, "an up-to-date install writes nothing at all")
        check(not (project / MARKER_REL).exists(), "an up-to-date install leaves no preview marker")

        # --- cannot-determine is not up-to-date ------------------------------
        print()
        print("cannot-determine path:")
        project = make_project(tmp_path, "no-baseline", stamp_sha=None)
        result = run(project, ["--engine-root", str(engine)], {"UPDATE_CHECK_UPSTREAM_CMD": up})
        combined = result.stdout + result.stderr
        check("reason=no_baseline_recorded" in combined,
              "a baseline-less install surfaces reason=no_baseline_recorded")
        check("already up to date" not in combined,
              "a baseline-less install is never reported as already up to date")
        check(result.returncode in (10, 20),
              f"a baseline-less install exits 10 (preview) or 20, not 0 (got {result.returncode})")

        if not have_rsync:
            print()
            print("SKIP (loud, not silent): rsync is not on PATH, so the preview/apply "
                  "assertions below cannot run. This is a skip, not a pass.",
                  file=sys.stderr)
        else:
            # --- first apply is a preview (acceptance 18) --------------------
            print()
            print("preview gate (acceptance 18):")
            project = make_project(tmp_path, "apply")
            state_dir = tmp_path / "state"
            (state_dir / "blackboard").mkdir(parents=True)
            sentinel = state_dir / "audit.jsonl"
            sentinel.write_text('{"kind":"sentinel"}\n')
            state_before = tree_digest(state_dir)
            state_mtime_before = sentinel.stat().st_mtime_ns

            local_before = {
                f: (project / ".autonomous-team" / f).read_bytes()
                for f in ("config.json", "project.json", "agent-profiles.json")
            }
            before = tree_digest(project)
            env = {"UPDATE_CHECK_UPSTREAM_CMD": up, "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir)}

            result = run(project, ["--engine-root", str(engine)], env)
            check(result.returncode == 10, "the first apply exits 10 (preview only)")
            check("preview only — nothing was written" in result.stdout,
                  "the first apply says nothing was written")
            check("would CREATE" in result.stdout, "the preview lists paths it would create")
            check("+ scripts/engine-new-tool.sh" in result.stdout,
                  "the preview names a specific path bootstrap would create")
            check("would REMOVE/ARCHIVE (0)" in result.stdout,
                  "the preview states that nothing is removed or archived")
            # The defect this section exists for: bootstrap's report of what an
            # apply will NOT do is printed only on its stdout, which the mirror
            # run captures to a log file. Surfacing that log only on failure
            # meant a complete-looking change set silently read as "your agent
            # definitions were updated" when they were deliberately skipped.
            check("Upstream agent-definition updates this apply will NOT take:" in result.stdout,
                  "the preview flags what the apply will not do")
            check("1 agent definition(s) have upstream updates you are not receiving:"
                  in result.stdout,
                  "the preview relays bootstrap's withheld-agent-updates report")
            check("    example.md" in result.stdout,
                  "the preview names the specific agent definition being left alone")
            # Deliberately NOT asserted: that the named file is absent from the
            # change set. The real bootstrap sweeps .claude/agents/ with the
            # repo-identifier rewrite (bootstrap.sh:1261) on every run, so on a
            # real install a withheld file legitimately appears in BOTH the
            # change set (rewritten) and this report (upstream content not
            # taken). This fixture models the install/skip pass, not the sed
            # sweep, so asserting absence here would encode an expectation the
            # real tool does not meet — measured on a real install during the
            # fix round's Gate 2, which is where that overlap was found.
            check(str(project) in result.stdout and "/mirror/" not in result.stdout,
                  "the report's paths are rewritten from the throwaway mirror to this repo")
            check("never updated by bootstrap after first install" in result.stdout,
                  "the preview states that CLAUDE.md is not updated by an apply")
            check((project / MARKER_REL).exists(), "the first apply records a preview marker")
            check(tree_digest(project, exclude={MARKER_REL}) == before,
                  "the first apply writes nothing but the preview marker")
            check(not (project / "scripts" / "engine-new-tool.sh").exists(),
                  "the first apply does not install the engine's new file")

            # --- a marker for a different engine does not authorize an apply --
            other_engine = make_engine(tmp_path / "other", "v3")
            other_sha = subprocess.run(["git", "-C", str(other_engine), "rev-parse", "HEAD"],
                                       capture_output=True, text=True, check=True).stdout.strip()
            result = run(project, ["--engine-root", str(other_engine)],
                         {"UPDATE_CHECK_UPSTREAM_CMD": upstream_cmd(other_sha),
                          "AUTONOMOUS_TEAM_STATE_DIR": str(state_dir)})
            check(result.returncode == 10,
                  "a preview marker for one engine commit does not authorize applying another")

            # Re-preview the original engine so the marker matches it again.
            run(project, ["--engine-root", str(engine)], env)

            # --- explicit --dry-run still writes nothing ---------------------
            before_dry = tree_digest(project, exclude={MARKER_REL})
            result = run(project, ["--engine-root", str(engine), "--dry-run"], env)
            check(result.returncode == 10, "--dry-run exits 10 even with a valid marker")
            check("dry run — nothing was written" in result.stdout, "--dry-run says nothing was written")
            check(tree_digest(project, exclude={MARKER_REL}) == before_dry,
                  "--dry-run writes nothing but the preview marker")

            # --- second apply writes for real (acceptance 17, 19, 22) --------
            print()
            print("real apply (acceptance 17, 19, 22):")
            result = run(project, ["--engine-root", str(engine)], env)
            check(result.returncode == 0, "the second apply exits 0")
            check((project / "scripts" / "engine-new-tool.sh").exists(),
                  "the second apply installs the engine's new file")
            check((project / "backend" / "engine_payload.py").read_text() == "PAYLOAD = 'v2'\n",
                  "the second apply overwrites an existing file with the engine's content")
            check(not (project / MARKER_REL).exists(),
                  "a successful apply consumes the preview marker")
            check((project / ".claude" / "agents" / "example.md").read_text()
                  == "# agent definition LOCALLY EDITED\n",
                  "the apply leaves the locally-edited agent definition alone, as the preview said")

            for f, want in local_before.items():
                check((project / ".autonomous-team" / f).read_bytes() == want,
                      f"local .autonomous-team/{f} is byte-identical after the apply")

            check(tree_digest(state_dir) == state_before,
                  "$AUTONOMOUS_TEAM_STATE_DIR is byte-identical after the apply")
            check(sentinel.stat().st_mtime_ns == state_mtime_before,
                  "$AUTONOMOUS_TEAM_STATE_DIR mtimes are untouched after the apply")

            stamp = json.loads((project / ".autonomous-team" / "engine-install.json").read_text())
            check(stamp.get("engine_commit") == engine_sha,
                  "the apply refreshes the baseline stamp to the applied engine commit")
            check("up to date" in result.stdout,
                  "the apply re-measures and reports up to date afterwards (acceptance 22)")

            # --- idempotence (acceptance 20) ---------------------------------
            print()
            print("idempotence (acceptance 20):")
            after_apply = tree_digest(project)
            result = run(project, ["--engine-root", str(engine)], env)
            check(result.returncode == 0, "an immediate second apply exits 0")
            check("already up to date" in result.stdout,
                  "an immediate second apply reports already up to date")
            check(tree_digest(project) == after_apply,
                  "an immediate second apply writes nothing (full recursive checksum)")

        # --- acceptance 21: the no-removal claim, asserted not assumed -------
        print()
        print("archive protocol (acceptance 21):")
        real_bootstrap = REPO_ROOT / "loop-bootstrap" / "bootstrap.sh"
        if not real_bootstrap.is_file():
            print("  SKIP (loud, not silent): loop-bootstrap/bootstrap.sh is not present in "
                  "this tree (an adopter install, where it never ships) — the source "
                  "assertions below cannot run here.", file=sys.stderr)
        else:
            body = real_bootstrap.read_text()
            rsync_lines = [ln for ln in body.splitlines() if re.search(r"\brsync\b", ln)
                           and not ln.lstrip().startswith("#")]
            check(bool(rsync_lines), "found rsync invocations in bootstrap.sh to check")
            check(all("--delete" not in ln for ln in rsync_lines),
                  "no rsync in bootstrap.sh carries --delete, so an update removes nothing")
            check(not re.search(r"^\s*git\s+rm\b", body, re.M),
                  "bootstrap.sh contains no `git rm`")
        apply_body = UPDATE_APPLY.read_text()
        check(not re.search(r"^\s*git\s+rm\b", apply_body, re.M),
              "update-apply.sh contains no `git rm`")
        check("bootstrap.sh" in apply_body and "--repo" in apply_body,
              "update-apply.sh invokes bootstrap.sh rather than reimplementing it")

    print()
    if FAIL:
        print(f"FAIL: {FAIL} assertion(s) failed, {PASS} passed:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"PASS: all {PASS} assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
