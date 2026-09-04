"""backend/orchestrator — Hybrid SDK orchestrator (Phase 1).

Routes agent spawns between the Anthropic Agent SDK path and the existing
claude-p/Agent() path.  Phase 1 runs in shadow mode: alternating Discussions
go through the SDK runner while the existing path continues to be the
behaviorally authoritative route.
"""
