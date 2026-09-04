#!/usr/bin/env bash
# scripts/coldstart-interview/repo-signals.sh
#
# Deterministic repo-signal detection for the agent-conducted coldstart
# interview (see wiki/Coldstart-Interview-Protocol.md). Reads the target
# repo's file tree, package manifests, README, and git remote to answer
# "what does this repo already tell us" so the interview never re-asks
# something the repo itself already answers.
#
# No network calls. No stdin reads. Read-only against the target path.
#
# Usage:
#   bash scripts/coldstart-interview/repo-signals.sh --repo-path <path>
#   bash scripts/coldstart-interview/repo-signals.sh --self-test
#
# Output (stdout): a single JSON object with at least these keys:
#   detected_languages  - array of language names, most-files-first
#   package_manager     - best-guess package manager id, or null
#   framework_guess     - best-guess framework name, or null
#   readme_present      - bool
#   readme_excerpt      - first few non-blank README lines (or "")
#   repo_owner          - git remote "origin" owner/org, or null

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: repo-signals.sh --repo-path <path>
       repo-signals.sh --self-test
EOF
}

detect_signals() {
  local repo_path="$1"
  python3 - "$repo_path" <<'PYEOF'
import json, os, re, subprocess, sys

repo_path = sys.argv[1]

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".claude"}
EXT_LANG = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java",
    ".c": "c", ".cpp": "c++", ".cc": "c++", ".sh": "shell", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".cs": "c#",
}


def walk_files(root, max_files=4000):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            out.append(os.path.join(dirpath, fn))
            if len(out) >= max_files:
                return out
    return out


def exists(*parts):
    return os.path.isfile(os.path.join(repo_path, *parts))


files = walk_files(repo_path) if os.path.isdir(repo_path) else []
lang_counts = {}
for f in files:
    _, ext = os.path.splitext(f)
    lang = EXT_LANG.get(ext)
    if lang:
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
detected_languages = [l for l, _ in sorted(lang_counts.items(), key=lambda kv: -kv[1])]

package_manager = None
if exists("package-lock.json"):
    package_manager = "npm"
elif exists("yarn.lock"):
    package_manager = "yarn"
elif exists("pnpm-lock.yaml"):
    package_manager = "pnpm"
elif exists("poetry.lock"):
    package_manager = "poetry"
elif exists("Pipfile.lock") or exists("Pipfile"):
    package_manager = "pipenv"
elif exists("requirements.txt"):
    package_manager = "pip"
elif exists("Cargo.lock") or exists("Cargo.toml"):
    package_manager = "cargo"
elif exists("go.sum") or exists("go.mod"):
    package_manager = "go modules"
elif exists("Gemfile.lock") or exists("Gemfile"):
    package_manager = "bundler"
elif exists("composer.lock") or exists("composer.json"):
    package_manager = "composer"

framework_guess = None
pkg_json_path = os.path.join(repo_path, "package.json")
if os.path.isfile(pkg_json_path):
    try:
        with open(pkg_json_path) as f:
            pkg = json.load(f)
        deps = {}
        deps.update(pkg.get("dependencies", {}) or {})
        deps.update(pkg.get("devDependencies", {}) or {})
        for name, guess in [
            ("next", "Next.js"), ("react", "React"), ("vue", "Vue"),
            ("@angular/core", "Angular"), ("svelte", "Svelte"),
            ("express", "Express"), ("fastify", "Fastify"),
        ]:
            if name in deps:
                framework_guess = guess
                break
    except (json.JSONDecodeError, OSError):
        pass
if framework_guess is None and exists("manage.py"):
    framework_guess = "Django"

readme_path = None
for cand in ("README.md", "README", "Readme.md", "readme.md"):
    if exists(cand):
        readme_path = os.path.join(repo_path, cand)
        break

readme_present = readme_path is not None
readme_excerpt = ""
if readme_present:
    try:
        with open(readme_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = [l for l in content.splitlines() if l.strip()]
        readme_excerpt = "\n".join(lines[:8])[:800]
    except OSError:
        readme_excerpt = ""

repo_owner = None
try:
    result = subprocess.run(
        ["git", "-C", repo_path, "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(\.git)?$", url)
        if m:
            repo_owner = m.group(1)
except (OSError, subprocess.SubprocessError):
    repo_owner = None

signals = {
    "detected_languages": detected_languages,
    "package_manager": package_manager,
    "framework_guess": framework_guess,
    "readme_present": readme_present,
    "readme_excerpt": readme_excerpt,
    "repo_owner": repo_owner,
}
print(json.dumps(signals, indent=2, sort_keys=True))
PYEOF
}

self_test() {
  local tmp
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  mkdir -p "$tmp/src"
  cat > "$tmp/package.json" <<'FIXEOF'
{"name": "fixture", "dependencies": {"react": "^18.0.0"}}
FIXEOF
  printf '{}\n' > "$tmp/package-lock.json"
  printf 'export const x = 1;\n' > "$tmp/src/index.tsx"
  cat > "$tmp/README.md" <<'FIXEOF'
# Fixture Project

A synthetic fixture repo for repo-signals.sh --self-test.

## Usage

Nothing to see here.
FIXEOF

  git -C "$tmp" init -q
  git -C "$tmp" remote add origin "https://github.com/acme-corp/fixture-repo.git"

  local out_file="$tmp.out.json"
  detect_signals "$tmp" > "$out_file"
  echo "[self-test] signals: $(cat "$out_file")"

  python3 - "$out_file" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert "typescript" in d["detected_languages"], d
assert d["package_manager"] == "npm", d
assert d["framework_guess"] == "React", d
assert d["readme_present"] is True, d
assert "Fixture Project" in d["readme_excerpt"], d
assert d["repo_owner"] == "acme-corp", d
print("ok")
PYEOF
  rm -f "$out_file"

  # Second fixture: no README, no package manifest, python-only, no git
  # remote -- checks the "nothing detected" branches don't blow up.
  local tmp2
  tmp2="$(mktemp -d)"
  mkdir -p "$tmp2/pkg"
  printf 'x = 1\n' > "$tmp2/pkg/main.py"
  git -C "$tmp2" init -q

  local out2
  out2="$(detect_signals "$tmp2")"
  echo "[self-test] bare-python signals: $out2"
  python3 -c "
import json, sys
d = json.loads(sys.argv[1])
assert d['detected_languages'] == ['python'], d
assert d['package_manager'] is None, d
assert d['readme_present'] is False, d
assert d['readme_excerpt'] == '', d
assert d['repo_owner'] is None, d
print('ok')
" "$out2"
  rm -rf "$tmp2"

  echo "[self-test] PASS"
}

# --- CLI dispatch --------------------------------------------------------

REPO_PATH=""
CMD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-path) CMD="detect"; REPO_PATH="$2"; shift 2 ;;
    --self-test) CMD="self-test"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

case "$CMD" in
  detect)
    [[ -n "$REPO_PATH" ]] || { echo "--repo-path required" >&2; exit 1; }
    detect_signals "$REPO_PATH"
    ;;
  self-test)
    self_test
    ;;
  *)
    usage
    exit 1
    ;;
esac
