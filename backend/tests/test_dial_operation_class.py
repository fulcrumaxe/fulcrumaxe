"""
Tests for backend.dial_operation_class.derive_class (D#1805).

Pure function — no state dir, no fixtures needed. The whole point of this
module is that a touchpoint under hooks/ classifies a spawn as
sandbox.modify regardless of role, and everything else falls through to the
existing role -> class mapping (which lands every role on agent.spawn today).
"""

from __future__ import annotations

from backend.dial_operation_class import derive_class


class TestHooksTouchpointClassifiesSandboxModify:
    def test_hooks_prefixed_touchpoint_maps_to_sandbox_modify(self):
        assert derive_class("executor", "hooks/sandbox_rules.py") == "sandbox.modify"

    def test_hooks_touchpoint_among_several(self):
        assert (
            derive_class("executor", "backend/foo.py,hooks/sandbox.py,scripts/bar.sh")
            == "sandbox.modify"
        )

    def test_hooks_touchpoint_as_list(self):
        assert derive_class("executor", ["hooks/sandbox.py"]) == "sandbox.modify"

    def test_mutated_prefix_no_longer_matches(self):
        # Mutation check (Spec item 7): change the prefix this touchpoint is
        # judged against and the classification must go red (no longer
        # sandbox.modify) — proves the assertion above is actually pinned to
        # the hooks/ prefix and not vacuously true.
        assert derive_class("executor", "hooksx/sandbox.py") != "sandbox.modify"


class TestNonSandboxTouchpointFallsThroughToRoleMapping:
    def test_backend_only_touchpoint_maps_to_agent_spawn(self):
        assert derive_class("executor", "backend/dial_registry.py") == "agent.spawn"

    def test_no_touchpoints_falls_through_to_role_mapping(self):
        # Spec item 3: the over-blocking guard — a spawn with no touchpoints
        # at all must be unaffected by this change.
        assert derive_class("executor", None) == "agent.spawn"
        assert derive_class("executor", "") == "agent.spawn"

    def test_unknown_role_defaults_to_agent_spawn(self):
        assert derive_class("some-made-up-role", "backend/foo.py") == "agent.spawn"


class TestOperationClassPrecedenceIsCallerConcern:
    # derive_class itself has no concept of an explicit --operation-class
    # override — that precedence lives in pre-spawn-check.sh (an honest
    # caller's explicit class always wins over any derivation). Documented
    # here so a reader of this test file isn't left wondering why there's no
    # override parameter on derive_class.
    def test_derive_class_has_no_override_parameter(self):
        import inspect

        sig = inspect.signature(derive_class)
        assert list(sig.parameters) == ["role", "touchpoints"]
