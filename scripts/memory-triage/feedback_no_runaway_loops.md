---
name: Runaway loop prevention is non-negotiable
description: Any code path that spawns `claude -p` or fires /loop must have a hard rate-limit and an external kill switch
type: feedback
originSessionId: f602f51e-d8cd-4b9e-8d85-4fb81c68c859
tier: hardwire-candidate
---
On 2026-05-10, `backend/api.py`'s `_innovate_tick` endpoint fanned out 16+ `claude -p "Run ONE /loop iteration..."` Opus subprocesses in a 2-minute burst, burning a large chunk of the user's plan credit before they noticed. Suspected trigger: a Puppeteer E2E test session hitting the dashboard's "Run loop" button after a recent fix made that button actually wire through to the backend.

**The rule:** every code path that can spawn a Claude Code subprocess (`claude -p`, `claude` CLI, `_start_loop_run`, `/loop` triggers) must have:
1. A wall-clock minimum interval between fires (e.g. ≥60s)
2. A concurrent-spawn cap (e.g. ≤1 in-flight at once)
3. A feature gate in `.autonomous-team/config.json` that disables it entirely
4. A visible counter so a human can see the fire rate before it goes wrong

**Why:** plan credits are a finite resource shared with the user's other work. A runaway in this repo doesn't just waste tokens — it locks the user out of Claude Code for hours. "It worked once" is not a basis for shipping a fan-out; the dangerous case is the second, third, and N-th call.

**How to apply:**
- When reviewing any PR that adds an HTTP endpoint, button, hotkey, or test that triggers `_start_loop_run` / `claude -p` / `/loop`: block until rate limit + concurrency cap are visible in the diff
- When reviewing Puppeteer/E2E specs: block any spec that clicks a "Run loop" or "Innovate" button without first stubbing/mocking the backend call
- Treat broken cron paths (e.g. the prompt-lane `ModuleNotFoundError` that ran every 10 min for hours) as P1 — silent failure of a scheduled job is its own bug class
- Don't restart `backend/api.py` until `_innovate_tick` and any sibling endpoints have all four protections above
