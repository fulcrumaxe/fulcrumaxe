"""Unit tests for the GraphQL API module (backend/graphql_api.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.graphql_api import _parse, execute, get_schema_types, ParseError  # noqa: E402


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParser:
    def test_simple_query(self):
        ast = _parse("{ health { ok } }")
        assert len(ast) == 1
        assert ast[0]["name"] == "health"
        assert ast[0]["sub"][0]["name"] == "ok"

    def test_nested_fields(self):
        ast = _parse("{ registry { discussions { number title } stats { total } } }")
        assert ast[0]["name"] == "registry"
        sub_names = [f["name"] for f in ast[0]["sub"]]
        assert "discussions" in sub_names
        assert "stats" in sub_names

    def test_arguments(self):
        ast = _parse('{ audit(limit: 5, source: "api") { timestamp action } }')
        field = ast[0]
        assert field["name"] == "audit"
        assert field["args"]["limit"] == 5
        assert field["args"]["source"] == "api"

    def test_aliases(self):
        ast = _parse("{ h: health { ok } b: budget { ceiling } }")
        assert ast[0]["alias"] == "h"
        assert ast[0]["name"] == "health"
        assert ast[1]["alias"] == "b"
        assert ast[1]["name"] == "budget"

    def test_parse_error_unmatched_brace(self):
        with pytest.raises(ParseError):
            _parse("{ health { ok }")  # missing closing brace

    def test_query_keyword(self):
        ast = _parse("query { health { ok } }")
        assert ast[0]["name"] == "health"

    def test_query_with_name(self):
        ast = _parse("query MyQuery { health { ok } }")
        assert ast[0]["name"] == "health"


# ---------------------------------------------------------------------------
# Executor / integration tests (resolvers mocked)
# ---------------------------------------------------------------------------

_MOCK_LOOP_HEALTH = {
    "healthy": True,
    "age_seconds": 42.0,
    "threshold_seconds": 600.0,
}

_MOCK_MODULE_HEALTH = {
    "modules": [
        {"name": "backend.budget", "healthy": True, "error": None},
    ]
}


def _mock_health_resolver(_args):
    return {
        "ok": True,
        "loop": {"age": 42.0, "threshold": 600.0, "healthy": True},
        "modules": [{"name": "backend.budget", "healthy": True, "error": None}],
    }


def _mock_budget_resolver(_args):
    return {
        "ceiling": 100000,
        "used": 12345,
        "remaining": 87655,
        "model": "claude-sonnet-4-5",
        "utilization_pct": 12.3,
    }


def _mock_registry_resolver(_args):
    return {
        "discussions": [
            {
                "number": 1,
                "title": "Test disc",
                "status": "DONE",
                "pr": 10,
                "created_at": "2026-01-01",
                "closed_at": None,
                "labels": [],
            }
        ],
        "stats": {"total": 1, "open": 0, "closed": 1, "velocity_7d": 0.5},
    }


def _mock_audit_resolver(args):
    return [
        {
            "timestamp": "2026-04-10T00:00:00Z",
            "source": "api",
            "action": "read",
            "actor": "test",
            "details": "{}",
        }
    ]


_PATCHED_RESOLVERS = {
    "health": _mock_health_resolver,
    "budget": _mock_budget_resolver,
    "registry": _mock_registry_resolver,
    "audit": _mock_audit_resolver,
}


@pytest.fixture(autouse=True)
def patch_resolvers(monkeypatch):
    """Patch _ROOT_RESOLVERS to avoid touching real backend modules."""
    import backend.graphql_api as gql
    monkeypatch.setattr(gql, "_ROOT_RESOLVERS", _PATCHED_RESOLVERS)


class TestSimpleQuery:
    def test_health_ok(self):
        resp = execute("{ health { ok } }")
        assert "errors" not in resp or not resp["errors"]
        assert resp["data"]["health"]["ok"] is True

    def test_only_requested_fields_returned(self):
        resp = execute("{ health { ok } }")
        # 'loop' was not requested — should not appear
        assert "loop" not in resp["data"]["health"]

    def test_budget_fields(self):
        resp = execute("{ budget { ceiling used remaining } }")
        b = resp["data"]["budget"]
        assert b["ceiling"] == 100000
        assert b["used"] == 12345
        assert "utilization_pct" not in b  # not requested


class TestNestedFields:
    def test_registry_discussions(self):
        resp = execute("{ registry { discussions { number title status } stats { total } } }")
        r = resp["data"]["registry"]
        assert isinstance(r["discussions"], list)
        assert r["discussions"][0]["number"] == 1
        assert r["discussions"][0]["title"] == "Test disc"
        # closed_at not requested
        assert "closed_at" not in r["discussions"][0]
        assert r["stats"]["total"] == 1


class TestArguments:
    def test_audit_with_args(self):
        resp = execute('{ audit(limit: 5, source: "api") { timestamp action } }')
        assert "errors" not in resp or not resp["errors"]
        entries = resp["data"]["audit"]
        assert isinstance(entries, list)
        assert entries[0]["timestamp"] == "2026-04-10T00:00:00Z"
        # 'source' not requested in sub-selection
        assert "source" not in entries[0]


class TestAliases:
    def test_aliases_appear_in_data(self):
        resp = execute("{ h: health { ok } b: budget { ceiling used } }")
        assert "h" in resp["data"]
        assert "b" in resp["data"]
        assert resp["data"]["h"]["ok"] is True
        assert resp["data"]["b"]["ceiling"] == 100000
        # canonical names should NOT appear under non-aliased keys
        assert "health" not in resp["data"]
        assert "budget" not in resp["data"]


class TestUnknownField:
    def test_unknown_root_field(self):
        resp = execute("{ nonExistentField { foo } }")
        assert "errors" in resp
        msgs = [e["message"] for e in resp["errors"]]
        assert any("nonExistentField" in m for m in msgs)

    def test_unknown_sub_field(self):
        resp = execute("{ health { ok unknownSubField } }")
        # data should still return ok, errors should mention unknownSubField
        assert resp["data"]["health"]["ok"] is True
        assert "errors" in resp
        assert any("unknownSubField" in e["message"] for e in resp["errors"])


class TestParseErrors:
    def test_unclosed_brace(self):
        resp = execute("{ health { ok }")
        assert "errors" in resp
        assert any("Parse error" in e["message"] for e in resp["errors"])

    def test_empty_query(self):
        resp = execute("{}")
        # Empty selection — returns empty data
        assert "data" in resp
        assert resp["data"] == {}


class TestIntrospection:
    def test_schema_types(self):
        resp = execute("{ __schema { types { name } } }")
        assert "errors" not in resp or not resp["errors"]
        type_names = [t["name"] for t in resp["data"]["__schema"]["types"]]
        assert "Query" in type_names
        assert "HealthStatus" in type_names
        assert "Discussion" in type_names

    def test_type_introspection(self):
        resp = execute('{ __type(name: "Discussion") { name fields { name } } }')
        assert "errors" not in resp or not resp["errors"]
        t = resp["data"]["__type"]
        assert t["name"] == "Discussion"
        field_names = [f["name"] for f in t["fields"]]
        assert "number" in field_names
        assert "title" in field_names


class TestGetSchemaTypes:
    def test_returns_all_types(self):
        types = get_schema_types()
        names = [t["name"] for t in types]
        assert "Query" in names
        assert "HealthStatus" in names

    def test_type_has_fields(self):
        types = get_schema_types()
        discussion = next(t for t in types if t["name"] == "Discussion")
        field_names = [f["name"] for f in discussion["fields"]]
        assert "number" in field_names
        assert "title" in field_names


class TestResolverError:
    def test_resolver_exception_returns_error(self, monkeypatch):
        """When a resolver raises, partial data is returned alongside errors."""
        import backend.graphql_api as gql
        bad_resolvers = dict(_PATCHED_RESOLVERS)
        bad_resolvers["health"] = lambda _: (_ for _ in ()).throw(RuntimeError("resolver boom"))
        monkeypatch.setattr(gql, "_ROOT_RESOLVERS", bad_resolvers)
        resp = execute("{ health { ok } }")
        assert "errors" in resp
        assert any("resolver boom" in e["message"] for e in resp["errors"])
