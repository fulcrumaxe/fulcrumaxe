"""
Human verification orchestrator.

Guides a human tester through the product one feature at a time.
Starts services, presents precise check instructions, collects PASS/FAIL
verdicts, auto-files SPEC_READY bug Discussions on failures, and tracks
everything in verification-report/human-checklist.json.

Usage:
    python backend/human_verification.py
    python backend/human_verification.py --checklist verification-report/human-checklist.json
    python backend/human_verification.py --skip-service-check
    python backend/human_verification.py --check-reverify
"""

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_CHECKLIST = Path("verification-report/human-checklist.json")

from backend._repo import REPO as _REPO  # noqa: E402
from backend._repo import REPO_OWNER as _REPO_OWNER, REPO_NAME as _REPO_NAME  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class HumanVerification:
    def __init__(self, checklist_path: Path, repo_root: Path):
        self.checklist_path = checklist_path
        self.repo_root = repo_root
        self.checklist: dict = {}
        self.run_id = _run_id()
        self._partial_results: list = []

    # ------------------------------------------------------------------
    # Checklist I/O
    # ------------------------------------------------------------------

    def load_checklist(self) -> dict:
        if self.checklist_path.exists():
            with open(self.checklist_path) as f:
                self.checklist = json.load(f)
        else:
            self.checklist = _default_checklist()
            self.checklist_path.parent.mkdir(parents=True, exist_ok=True)
            self.save_checklist()
            print(f"Created new checklist at {self.checklist_path}")
        return self.checklist

    def save_checklist(self):
        self.checklist["last_run"] = _now_iso()
        with open(self.checklist_path, "w") as f:
            json.dump(self.checklist, f, indent=2)

    # ------------------------------------------------------------------
    # Item queries
    # ------------------------------------------------------------------

    def pending_items(self) -> list:
        """Return items with status 'pending' or 're-verify'."""
        return [
            item
            for item in self.checklist.get("items", [])
            if item.get("status") in ("pending", "re-verify")
        ]

    def _find_item(self, item_id: str) -> dict | None:
        for item in self.checklist.get("items", []):
            if item["id"] == item_id:
                return item
        return None

    # ------------------------------------------------------------------
    # Verification interaction
    # ------------------------------------------------------------------

    def run_item(self, item: dict) -> str:
        """Present an item to the human. Returns 'pass', 'fail', or 'skip'."""
        status_tag = " [RE-VERIFY]" if item.get("status") == "re-verify" else ""
        print("\n" + "=" * 70)
        print(f"  [{item['subsystem']}]{status_tag}  {item['id']}")
        print("=" * 70)
        print(f"\nDescription : {item['description']}")
        if item.get("setup"):
            print(f"Setup       : {item['setup']}")
        if item.get("check_url"):
            print(f"URL         : {item['check_url']}")
        print(f"\nInstructions: {item['instructions']}")
        print(f"Expected    : {item['expected']}")
        if item.get("bug_discussion"):
            print(f"Previous bug: Discussion #{item['bug_discussion']}")
        print()

        while True:
            try:
                answer = input("Result? [pass / fail / skip / quit] : ").strip().lower()
            except EOFError:
                answer = "quit"

            if answer in ("pass", "p"):
                note = input("Optional note (Enter to skip): ").strip()
                self.record_pass(item, note)
                return "pass"
            elif answer in ("fail", "f"):
                description = ""
                while not description:
                    description = input("Describe what's wrong: ").strip()
                    if not description:
                        print("Description is required for FAIL.")
                disc_num = self.record_fail(item, description)
                print(f"\nBug filed as Discussion #{disc_num} (SPEC_READY)")
                return "fail"
            elif answer in ("skip", "s"):
                item["status"] = "skip"
                self.save_checklist()
                print("Skipped.")
                return "skip"
            elif answer in ("quit", "q", "exit"):
                print("\nSaving partial progress and exiting...")
                self.save_checklist()
                sys.exit(0)
            else:
                print("Please enter: pass, fail, skip, or quit")

    # ------------------------------------------------------------------
    # Recording results
    # ------------------------------------------------------------------

    def record_pass(self, item: dict, note: str = ""):
        item["status"] = "pass"
        item["verified_by"] = "human"
        item["verified_at"] = _now_iso()
        item["note"] = note
        item["run_id"] = self.run_id
        self.save_checklist()
        self._partial_results.append({"id": item["id"], "result": "pass", "note": note})
        print(f"  PASS recorded for {item['id']}")

    def record_fail(self, item: dict, description: str) -> int:
        disc_num = self.auto_file_bug(item, description)
        item["status"] = "fail"
        item["verified_by"] = "human"
        item["verified_at"] = _now_iso()
        item["note"] = description
        item["bug_discussion"] = disc_num
        item["run_id"] = self.run_id
        self.save_checklist()
        self._partial_results.append(
            {"id": item["id"], "result": "fail", "description": description, "bug_discussion": disc_num}
        )
        return disc_num

    # ------------------------------------------------------------------
    # Auto-bug-filing
    # ------------------------------------------------------------------

    def auto_file_bug(self, item: dict, description: str) -> int:
        """File a SPEC_READY Discussion for a failed checklist item. Returns Discussion number."""
        now = _now_iso()
        technical_context = self._gather_technical_context(item)

        body = f"""<!-- STATUS:SPEC_READY SINCE:{now} -->

## Bug: {item['description']}

**Found by:** Human verification run {self.run_id}
**Checklist item:** {item['id']}
**Subsystem:** {item['subsystem']}

### Steps to Reproduce
1. {item.get('setup', 'N/A')}
2. {item['instructions']}

### Expected
{item['expected']}

### Actual (human report)
{description}

---

## Spec

### Summary
Fix {item['id']}: {description[:120]}

### Requirements
1. {item['description']} must work as expected.
2. Human verification for `{item['id']}` must produce PASS after fix.

### Acceptance Criteria
1. Re-running `bash scripts/human-verify.sh` for checklist item `{item['id']}` produces PASS.
2. The check URL (if applicable: `{item.get('check_url', 'N/A')}`) loads without errors.
3. The expected behaviour is visible: {item['expected']}

### Technical Solution
{technical_context}

### Estimate
~30–80 lines changed across 1–2 files
"""

        title = f"Bug: {item['subsystem']} — {item['description'][:60]}"
        cmd = [
            "gh", "api", "graphql",
            "--repo", _REPO,
            "-f", f"query=mutation CreateDiscussion($repoId:ID!, $categoryId:ID!, $title:String!, $body:String!) {{"
                  f"  createDiscussion(input:{{repositoryId:$repoId, categoryId:$categoryId, title:$title, body:$body}}) {{"
                  f"    discussion {{ number }}"
                  f"  }}"
                  f"}}",
        ]

        # Get repo ID and a valid category ID first
        repo_query = subprocess.run(
            [
                "gh", "api", "graphql",
                "--repo", _REPO,
                "-f",
                f"query=query {{ repository(owner:\"{_REPO_OWNER}\", name:\"{_REPO_NAME}\") {{ id discussionCategories(first:10) {{ nodes {{ id name }} }} }} }}",
            ],
            capture_output=True,
            text=True,
        )
        repo_data = json.loads(repo_query.stdout)
        repo_id = repo_data["data"]["repository"]["id"]

        categories = repo_data["data"]["repository"]["discussionCategories"]["nodes"]
        # Prefer "General" or first available category
        category_id = None
        for cat in categories:
            if cat["name"].lower() in ("general", "ideas", "q&a"):
                category_id = cat["id"]
                break
        if category_id is None and categories:
            category_id = categories[0]["id"]

        if not category_id:
            print("WARNING: No Discussion category found. Bug not filed.")
            return 0

        create_result = subprocess.run(
            [
                "gh", "api", "graphql",
                "--repo", _REPO,
                "-f",
                f"query=mutation {{ createDiscussion(input:{{repositoryId:\"{repo_id}\", categoryId:\"{category_id}\", "
                f"title:{json.dumps(title)}, body:{json.dumps(body)}}}) {{ discussion {{ number }} }} }}",
            ],
            capture_output=True,
            text=True,
        )

        if create_result.returncode != 0:
            print(f"WARNING: Failed to file bug Discussion: {create_result.stderr}")
            return 0

        result_data = json.loads(create_result.stdout)
        disc_num = result_data["data"]["createDiscussion"]["discussion"]["number"]
        return disc_num

    def _gather_technical_context(self, item: dict) -> str:
        """Look at relevant source files for the failed item and return context."""
        subsystem = item.get("subsystem", "").lower()
        lines = []

        if "dashboard" in subsystem or "python" in subsystem or "api" in subsystem:
            lines.append("Relevant files: backend/dashboard.py, backend/api_routes.py, backend/kpi_engine.py")
        if "rust" in subsystem or "saas" in subsystem:
            lines.append("Relevant files: saas-service/src/main.rs, saas-service/src/routes/")
        if "tui" in subsystem:
            lines.append("Relevant files: tui/src/App.tsx, tui/src/AgentFeed.tsx")
        if "react" in subsystem:
            lines.append("Relevant files: dashboard/src/")
        if "integration" in subsystem:
            lines.append("Relevant files: backend/api_routes.py, saas-service/src/")

        if not lines:
            lines.append("Check the relevant source files for this subsystem.")

        lines.append(f"\nCheck URL: {item.get('check_url', 'N/A')}")
        lines.append(f"Expected behaviour: {item['expected']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Re-verification tracking
    # ------------------------------------------------------------------

    def check_reverify_needed(self):
        """
        Scan GitHub for Discussions that were auto-filed by this system and
        are now DONE/RESOLVED. Mark corresponding checklist items as 're-verify'.
        """
        print("Checking for items needing re-verification...")
        items_with_bugs = [
            item
            for item in self.checklist.get("items", [])
            if item.get("bug_discussion") and item.get("status") == "fail"
        ]

        if not items_with_bugs:
            print("No failed items with filed bugs found.")
            return

        for item in items_with_bugs:
            disc_num = item["bug_discussion"]
            result = subprocess.run(
                [
                    "gh", "api", "graphql",
                    "--repo", _REPO,
                    "-f",
                    f"query=query {{ repository(owner:\"{_REPO_OWNER}\", name:\"{_REPO_NAME}\") {{ discussion(number:{disc_num}) {{ body closed }} }} }}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            discussion = data.get("data", {}).get("repository", {}).get("discussion")
            if not discussion:
                continue

            body = discussion.get("body", "")
            closed = discussion.get("closed", False)

            if closed or "STATUS:DONE" in body:
                item["status"] = "re-verify"
                print(f"  Marking {item['id']} for re-verification (Discussion #{disc_num} is resolved)")

        self.save_checklist()

    # ------------------------------------------------------------------
    # Proof report
    # ------------------------------------------------------------------

    def write_proof(self, run_results: list):
        """Write a summary report to verification-report/proof/{run_id}/human-results.json."""
        proof_dir = self.repo_root / "verification-report" / "proof" / self.run_id
        proof_dir.mkdir(parents=True, exist_ok=True)

        passed = sum(1 for r in run_results if r.get("result") == "pass")
        failed = sum(1 for r in run_results if r.get("result") == "fail")
        skipped = sum(1 for r in run_results if r.get("result") == "skip")

        report = {
            "run_id": self.run_id,
            "timestamp": _now_iso(),
            "summary": {
                "total": len(run_results),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            },
            "results": run_results,
        }

        report_path = proof_dir / "human-results.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        print(f"\nProof report written to {report_path}")
        return report_path

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        """Main entry point: iterate pending items, prompt human, record results."""
        # Reset per-run state so a reused instance doesn't accumulate results
        # across multiple run() calls.
        self._partial_results = []
        self.run_id = _run_id()
        self.load_checklist()
        self.check_reverify_needed()

        pending = self.pending_items()
        if not pending:
            print("\nNothing to verify — all items are up to date.")
            print("Run with --check-reverify to scan for newly resolved bugs.")
            return

        print(f"\n{'='*70}")
        print(f"  Human Verification Run: {self.run_id}")
        print(f"  Items to verify: {len(pending)}")
        print(f"{'='*70}")
        print("Commands: pass (p), fail (f), skip (s), quit (q)")
        print("Ctrl+C at any time saves partial progress.")

        # Handle Ctrl+C gracefully
        def _sigint_handler(sig, frame):
            print("\n\nInterrupted. Saving partial progress...")
            self.save_checklist()
            if self._partial_results:
                self.write_proof(self._partial_results)
            sys.exit(0)

        signal.signal(signal.SIGINT, _sigint_handler)

        run_results = []
        for item in pending:
            result = self.run_item(item)
            run_results.append({"id": item["id"], "result": result})

        # Summary
        passed = sum(1 for r in run_results if r["result"] == "pass")
        failed = sum(1 for r in run_results if r["result"] == "fail")
        skipped = sum(1 for r in run_results if r["result"] == "skip")

        print(f"\n{'='*70}")
        print(f"  Run complete: {passed} passed, {failed} failed, {skipped} skipped")
        print(f"{'='*70}")

        self.write_proof(self._partial_results or run_results)


# ------------------------------------------------------------------
# Default checklist — seeded with items for ALL subsystems
# ------------------------------------------------------------------

def _default_checklist() -> dict:
    return {
        "version": "1.0",
        "last_run": "",
        "items": [
            # ---- Python Backend API ----
            {
                "id": "hv-api-dashboard-loads",
                "subsystem": "Python Backend API",
                "description": "Dashboard HTML loads at /dashboard without errors",
                "setup": "Ensure Python API is running: python3 backend/api.py (or check port 18099)",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Open the URL in a browser. Does the page load with visible cards (no blank screen, no 404)?",
                "expected": "Dashboard HTML renders with card elements visible",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-kpi-card",
                "subsystem": "Python Backend API",
                "description": "KPI card on /dashboard shows cycle time and velocity numbers",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Open the dashboard. Look at the KPI card. Does it show numeric values for cycle time and PRs merged, not 'N/A' or blank?",
                "expected": "KPI card displays real numbers for cycle time and velocity",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-budget-card",
                "subsystem": "Python Backend API",
                "description": "Budget card on /dashboard shows spend numbers",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look at the Budget card. Does it show actual spend numbers (not 0/0 or blank)?",
                "expected": "Budget card shows session ceiling and amount spent",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-agents-card",
                "subsystem": "Python Backend API",
                "description": "Agent status card shows agent list",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look at the Agents card. Is there a list of agents (executor, reviewer, etc.) with their current status?",
                "expected": "Agent status card lists known agent roles with status",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-queue-card",
                "subsystem": "Python Backend API",
                "description": "Queue card shows Discussions count",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look at the Queue card. Does it show a count of pending, implementing, and spec-ready items?",
                "expected": "Queue card shows non-empty counts of Discussion states",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-loop-health-card",
                "subsystem": "Python Backend API",
                "description": "Loop health card shows last run time",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look at the Loop Health card. Does it show a last-run timestamp and loop status (running/idle)?",
                "expected": "Loop health card shows meaningful status, not blank",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-connection-dot",
                "subsystem": "Python Backend API",
                "description": "Connection indicator turns green when API is live",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look for a status dot or connection indicator (usually in the header/status bar). Is it green when the API is running?",
                "expected": "Connection indicator shows green/connected state",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-dashboard-modules-card",
                "subsystem": "Python Backend API",
                "description": "Module health card shows module statuses",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Look at the Module Health card. Does it show a list of backend modules (budget, registry, kpi, etc.) with their health status?",
                "expected": "Module health card shows multiple modules with pass/fail indicators",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-swagger-interactive",
                "subsystem": "Python Backend API",
                "description": "Swagger UI at /docs allows trying endpoints",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/docs",
                "instructions": "Open /docs. Click on the GET /health endpoint. Click 'Try it out', then 'Execute'. Does it return 200?",
                "expected": "Swagger UI renders, endpoint execution returns 200 OK",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-api-registry-data",
                "subsystem": "Python Backend API",
                "description": "/v1/registry returns actual Discussion data (not empty array)",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/v1/registry",
                "instructions": "Open the URL or run: curl http://localhost:18099/v1/registry. Does it return a non-empty JSON array with Discussion items?",
                "expected": "JSON array with at least a few Discussion items",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-api-kpi-data",
                "subsystem": "Python Backend API",
                "description": "/v1/kpi returns non-zero metrics",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/v1/kpi",
                "instructions": "Open the URL or run: curl http://localhost:18099/v1/kpi. Do the metrics contain non-zero values?",
                "expected": "KPI JSON with numeric fields, at least some non-zero",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-control-gate-toggle",
                "subsystem": "Python Backend API",
                "description": "Setting a gate via /v1/control/set is reflected in /v1/control/gates",
                "setup": "Ensure Python API is running on port 18099",
                "check_url": "http://localhost:18099/v1/control/gates",
                "instructions": "Run: curl -X POST http://localhost:18099/v1/control/set -d '{\"key\":\"gates.wiki_sync\",\"value\":false}' -H 'Content-Type: application/json'. Then GET /v1/control/gates and check wiki_sync is false.",
                "expected": "Gate change is reflected immediately in /v1/control/gates response",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-error-state-api-down",
                "subsystem": "Python Backend API",
                "description": "Dashboard shows error state when API is stopped",
                "setup": "Stop the Python API process",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Stop the Python API, then reload the dashboard page. Does it show a meaningful error state (not just a blank page or uncaught error)?",
                "expected": "Dashboard shows 'API unavailable' or similar error message",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            # ---- Rust SaaS Service ----
            {
                "id": "hv-rust-health",
                "subsystem": "Rust SaaS Service",
                "description": "Rust service health endpoint returns 200",
                "setup": "Ensure Rust service is running: cd saas-service && cargo run (or check port 3000)",
                "check_url": "http://localhost:3000/health",
                "instructions": "Run: curl http://localhost:3000/health. Does it return 200 with {\"ok\":true} or similar?",
                "expected": "HTTP 200 with JSON health response",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-rust-auth-register",
                "subsystem": "Rust SaaS Service",
                "description": "Rust auth — user registration works",
                "setup": "Ensure Rust service is running on port 3000",
                "check_url": "http://localhost:3000/auth/register",
                "instructions": "Run: curl -X POST http://localhost:3000/auth/register -d '{\"email\":\"test@test.com\",\"password\":\"testpass123\"}' -H 'Content-Type: application/json'. Does it return a token or success response?",
                "expected": "HTTP 200/201 with JWT token or user object",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-rust-projects-crud",
                "subsystem": "Rust SaaS Service",
                "description": "Rust /projects endpoint returns list",
                "setup": "Ensure Rust service is running on port 3000 and you have a valid auth token",
                "check_url": "http://localhost:3000/projects",
                "instructions": "Run: curl http://localhost:3000/projects -H 'Authorization: Bearer <token>'. Does it return a JSON array (even if empty)?",
                "expected": "HTTP 200 with JSON array of projects",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-rust-agents-endpoint",
                "subsystem": "Rust SaaS Service",
                "description": "Rust /agents endpoint returns agent list",
                "setup": "Ensure Rust service is running on port 3000",
                "check_url": "http://localhost:3000/agents",
                "instructions": "Run: curl http://localhost:3000/agents. Does it return JSON (array or object) with agent data?",
                "expected": "HTTP 200 with agent data",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-rust-websocket",
                "subsystem": "Rust SaaS Service",
                "description": "Rust WebSocket endpoint accepts connections",
                "setup": "Ensure Rust service is running on port 3000",
                "check_url": "ws://localhost:3000/ws",
                "instructions": "Run: node -e \"const ws=new (require('ws'))('ws://localhost:3000/ws'); ws.on('open',()=>{console.log('connected');ws.close()}); ws.on('error',e=>console.error(e.message))\" (requires ws package). Or use a WebSocket test tool.",
                "expected": "WebSocket connection accepted (or returns 101 Switching Protocols)",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-rust-billing-endpoint",
                "subsystem": "Rust SaaS Service",
                "description": "Rust billing/metering endpoint responds",
                "setup": "Ensure Rust service is running on port 3000",
                "check_url": "http://localhost:3000/billing",
                "instructions": "Run: curl http://localhost:3000/billing. Does it return any response (200 or auth error, not 404)?",
                "expected": "HTTP 200 or 401 (any non-404 response means route exists)",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            # ---- TUI ----
            {
                "id": "hv-tui-starts",
                "subsystem": "TUI",
                "description": "TUI starts without crashing",
                "setup": "Build the TUI: cd tui && npm run build",
                "check_url": "",
                "instructions": "Run: cd tui && npm start (or node dist/index.js). Does it start without immediately crashing? Does it show the agent feed or status bar?",
                "expected": "TUI renders in the terminal without error on startup",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-tui-accepts-input",
                "subsystem": "TUI",
                "description": "TUI accepts keyboard input",
                "setup": "TUI is running",
                "check_url": "",
                "instructions": "With the TUI running, type a few characters. Does the input appear in the chat input area? Press Enter — does something happen (even if it shows 'no response')?",
                "expected": "Input field accepts keystrokes and Enter submits",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-tui-agent-feed",
                "subsystem": "TUI",
                "description": "TUI agent feed renders output",
                "setup": "TUI is running and connected to backend",
                "check_url": "",
                "instructions": "If there are any agent messages in the feed, can you see them? Does the feed area show something (even 'no messages yet') rather than a blank area?",
                "expected": "Agent feed area renders with content or placeholder message",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-tui-status-bar",
                "subsystem": "TUI",
                "description": "TUI status bar shows loop status",
                "setup": "TUI is running",
                "check_url": "",
                "instructions": "Look at the status bar (usually at the bottom). Does it show loop status, agent count, or token usage?",
                "expected": "Status bar shows meaningful system status information",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            # ---- React Dashboard ----
            {
                "id": "hv-react-builds",
                "subsystem": "React Dashboard",
                "description": "React dashboard builds without errors",
                "setup": "Run: cd dashboard && npm install && npm run build",
                "check_url": "",
                "instructions": "Run the build command. Does it complete with exit code 0? Are there TypeScript or build errors?",
                "expected": "Build completes with exit code 0, no errors",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-react-dashboard-renders",
                "subsystem": "React Dashboard",
                "description": "React dashboard renders at localhost:5173",
                "setup": "Run: cd dashboard && npm run dev",
                "check_url": "http://localhost:5173",
                "instructions": "Open http://localhost:5173 in a browser. Does the page load with visible UI components?",
                "expected": "React dashboard renders with visible cards or content",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-react-live-data",
                "subsystem": "React Dashboard",
                "description": "React dashboard shows live data from Python API",
                "setup": "Python API running on 18099, React dev server running on 5173",
                "check_url": "http://localhost:5173",
                "instructions": "With both services running, open the React dashboard. Does it show real data (not mock/empty), pulled from the Python API?",
                "expected": "Dashboard cards show data sourced from the Python API (not all zeros or mock values)",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-react-filters",
                "subsystem": "React Dashboard",
                "description": "React dashboard filters work",
                "setup": "React dashboard is running and showing data",
                "check_url": "http://localhost:5173",
                "instructions": "If there are filter controls (by subsystem, status, date range), try changing one. Does the displayed data update?",
                "expected": "Filter changes update the displayed data without page reload",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            # ---- Integration ----
            {
                "id": "hv-integration-data-consistency",
                "subsystem": "Integration",
                "description": "Python dashboard and React dashboard show consistent data",
                "setup": "Both Python API (18099) and React dashboard (5173) are running",
                "check_url": "http://localhost:18099/dashboard",
                "instructions": "Check the KPI numbers shown on the Python dashboard (/dashboard) and the React dashboard (localhost:5173). Are the numbers roughly consistent (same order of magnitude, not wildly different)?",
                "expected": "Both dashboards source from the same backend data — numbers should be consistent",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-integration-rust-dispatch",
                "subsystem": "Integration",
                "description": "Rust service processes agent dispatch events",
                "setup": "Rust service (3000) and Python API (18099) are both running",
                "check_url": "",
                "instructions": "Run: curl -X POST http://localhost:3000/dispatch -d '{\"agent\":\"executor\",\"task\":\"test\"}' -H 'Content-Type: application/json'. Does it return a success or acknowledgement response?",
                "expected": "Dispatch endpoint acknowledges the event (200 or 202)",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
            {
                "id": "hv-integration-budget-propagation",
                "subsystem": "Integration",
                "description": "Budget updates are visible in both dashboard and API",
                "setup": "Python API running on 18099",
                "check_url": "http://localhost:18099/v1/budget/status",
                "instructions": "Run: python3 backend/budget.py spend test-agent executor 1000 500. Then check /v1/budget/status — does the spend number update? Also refresh /dashboard — does the budget card update?",
                "expected": "Budget spend is reflected in API response and dashboard within one refresh",
                "status": "pending",
                "verified_by": "",
                "verified_at": "",
                "note": "",
                "bug_discussion": None,
                "fix_pr": None,
                "run_id": "",
            },
        ]
    }


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Human-guided verification orchestrator"
    )
    parser.add_argument(
        "--checklist",
        default=str(_DEFAULT_CHECKLIST),
        help="Path to human-checklist.json (default: verification-report/human-checklist.json)",
    )
    parser.add_argument(
        "--skip-service-check",
        action="store_true",
        help="Skip service health check before running",
    )
    parser.add_argument(
        "--check-reverify",
        action="store_true",
        help="Only check for items needing re-verification (don't run interactive session)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    checklist_path = Path(args.checklist)
    if not checklist_path.is_absolute():
        checklist_path = repo_root / checklist_path

    hv = HumanVerification(checklist_path=checklist_path, repo_root=repo_root)
    hv.load_checklist()

    if args.check_reverify:
        hv.check_reverify_needed()
        print("Re-verification check complete.")
        return

    hv.run()


if __name__ == "__main__":
    main()
