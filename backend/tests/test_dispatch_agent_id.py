"""Tests for backend/orchestrator/dispatch.py::_make_agent_id and
_record_cc_route's id/role formatting.

`dict.get(key, default)` only returns `default` when `key` is absent — a
key present with value `None` returns `None`, not the default. These tests
pin the fix (`.get(key) or default`) at the three sites that build
`agent_run.agent_id` / `agent_run.role`, per D#1973.
"""
from __future__ import annotations

import re
from unittest.mock import patch

from backend.orchestrator.dispatch import _make_agent_id, _record_cc_route

# Grammar from scripts/lib/transcript_event_id.py:62
_CANONICAL_ID = re.compile(r"^[a-z]+(?:-[a-z]+)*-(?:[0-9]+|nod)-[0-9]{9,12}$")


def test_present_none_discussion_yields_nod_sentinel():
    agent_id = _make_agent_id({"role": "executor", "discussion": None})
    assert "-None-" not in agent_id
    assert re.match(r"^executor-nod-[0-9]{9,12}$", agent_id), agent_id


def test_present_none_role_yields_unknown_sentinel():
    agent_id = _make_agent_id({"role": None, "discussion": 1973})
    assert not agent_id.startswith("None-")
    assert re.match(r"^unknown-1973-[0-9]{9,12}$", agent_id), agent_id


def test_all_id_shapes_are_canonical():
    inputs = [
        {},
        {"role": None},
        {"discussion": None},
        {"role": None, "discussion": None},
        {"role": "executor", "discussion": 1973},
    ]
    for spec_dict in inputs:
        agent_id = _make_agent_id(spec_dict)
        assert _CANONICAL_ID.match(agent_id), (spec_dict, agent_id)


def test_discussion_zero_folds_to_nod():
    agent_id = _make_agent_id({"role": "executor", "discussion": 0})
    assert re.match(r"^executor-nod-[0-9]{9,12}$", agent_id), agent_id


def test_record_cc_route_never_passes_none_role():
    with patch(
        "backend.agent_run_tracker.start_run"
    ) as mock_start_run, patch("backend.agent_run_tracker.complete_run"):
        _record_cc_route(
            "unknown-nod-1788849420", {"role": None, "discussion": None}
        )

    assert mock_start_run.called
    _, kwargs = mock_start_run.call_args
    assert kwargs["role"] == "unknown"
    assert kwargs["role"] is not None


def test_present_none_discussion_collides_with_nod_at_same_second():
    """Documents, deliberately, that the fix does not close the Spec's
    key-space collision -- it folds it into the pre-existing `nod` class.

    Before the fix, two same-second dispatches with `discussion: None`
    collided because both produced the literal string `None` in the id
    (the live table holds `executor-None-1787187948` / `...949` as proof).
    After the fix, they still collide -- they now both fold to the `nod`
    sentinel, which is the same collision class that absent-discussion
    dispatches have always had (171 `-nod-` rows already share this
    property and it is accepted, per the Spec's discussion of `nod` as a
    legitimate, blessed state). The residual risk is real but not new: it
    is bounded by whatever tolerance already exists for `nod` collisions,
    not created fresh by this fix.
    """
    with patch("time.time", return_value=1788849420.0):
        first = _make_agent_id({"role": "executor", "discussion": None})
        second = _make_agent_id({"role": "executor", "discussion": None})

    assert first == second == "executor-nod-1788849420"
