"""
GraphQL API — minimal /graphql endpoint built on stdlib.

Implements a recursive-descent parser for the subset of GraphQL needed:
  - Field selection (nested)
  - Arguments on fields  (e.g. audit(limit:10, source:"api"))
  - Aliases              (e.g. h: health { ok })
  - Introspection        (__schema, __type)

NOT supported (intentionally): fragments, variables, mutations, subscriptions,
directives.  The grammar we parse is small enough to handle in ~120 lines.

Public interface:
    execute(query_str, resolvers=None) -> {"data": {...}} | {"errors": [...]}
    get_schema_types()                 -> list of type dicts (for introspection)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Schema definition (documentation + runtime type registry)
# ---------------------------------------------------------------------------

SCHEMA_SDL = """
type Query {
  health: HealthStatus
  budget: BudgetStatus
  cost: CostSummary
  registry: Registry
  agents: AgentList
  kpi: KPI
  control: ControlInfo
  audit(source: String, action: String, actor: String, since: String, limit: Int): [AuditEntry]
  replays: ReplayList
  spawnQueue: SpawnQueue
  notifications: NotificationHistory
  plugins: PluginList
}

type HealthStatus {
  ok: Boolean
  loop: LoopHealth
  modules: [ModuleHealth]
}

type LoopHealth {
  age: Float
  threshold: Float
  healthy: Boolean
}

type ModuleHealth {
  name: String
  healthy: Boolean
  error: String
}

type BudgetStatus {
  ceiling: Int
  used: Int
  remaining: Int
  model: String
  utilization_pct: Float
}

type CostSummary {
  total_usd: Float
  by_model: [ModelCost]
  by_agent: [AgentCost]
}

type ModelCost {
  model: String
  cost: Float
}

type AgentCost {
  agent_id: String
  role: String
  cost: Float
}

type Registry {
  discussions: [Discussion]
  stats: RegistryStats
}

type Discussion {
  number: Int
  title: String
  status: String
  pr: Int
  created_at: String
  closed_at: String
  labels: [String]
}

type RegistryStats {
  total: Int
  open: Int
  closed: Int
  velocity_7d: Float
}

type AgentList {
  agents: [Agent]
}

type Agent {
  role: String
  description: String
  status: String
  tools: [String]
  review_pipeline: String
}

type KPI {
  velocity: KPIVelocity
  cycle_time: KPICycleTime
}

type KPIVelocity {
  prs_7d: Int
  prs_30d: Int
}

type KPICycleTime {
  median_hours: Float
  p95_hours: Float
}

type ControlInfo {
  gates: [ControlGate]
}

type ControlGate {
  key: String
  value: String
}

type AuditEntry {
  timestamp: String
  source: String
  action: String
  actor: String
  details: String
}

type ReplayList {
  replays: [ReplayMeta]
}

type ReplayMeta {
  agent_id: String
  role: String
  discussion: String
  started_at: String
  duration_s: Float
  event_count: Int
}

type SpawnQueue {
  pending_count: Int
  active_count: Int
  utilization_pct: Float
}

type NotificationHistory {
  notifications: [String]
}

type PluginList {
  plugins: [Plugin]
}

type Plugin {
  name: String
  description: String
  version: String
  review_pipeline: String
  tools: [String]
}
"""

# Runtime type registry — maps type name → field names (used for introspection)
_TYPE_FIELDS: dict[str, list[str]] = {
    "Query": [
        "health", "budget", "cost", "registry", "agents", "kpi",
        "control", "audit", "replays", "spawnQueue", "notifications", "plugins",
    ],
    "HealthStatus": ["ok", "loop", "modules"],
    "LoopHealth": ["age", "threshold", "healthy"],
    "ModuleHealth": ["name", "healthy", "error"],
    "BudgetStatus": ["ceiling", "used", "remaining", "model", "utilization_pct"],
    "CostSummary": ["total_usd", "by_model", "by_agent"],
    "ModelCost": ["model", "cost"],
    "AgentCost": ["agent_id", "role", "cost"],
    "Registry": ["discussions", "stats"],
    "Discussion": ["number", "title", "status", "pr", "created_at", "closed_at", "labels"],
    "RegistryStats": ["total", "open", "closed", "velocity_7d"],
    "AgentList": ["agents"],
    "Agent": ["role", "description", "status", "tools", "review_pipeline"],
    "KPI": ["velocity", "cycle_time"],
    "KPIVelocity": ["prs_7d", "prs_30d"],
    "KPICycleTime": ["median_hours", "p95_hours"],
    "ControlInfo": ["gates"],
    "ControlGate": ["key", "value"],
    "AuditEntry": ["timestamp", "source", "action", "actor", "details"],
    "ReplayList": ["replays"],
    "ReplayMeta": ["agent_id", "role", "discussion", "started_at", "duration_s", "event_count"],
    "SpawnQueue": ["pending_count", "active_count", "utilization_pct"],
    "NotificationHistory": ["notifications"],
    "PluginList": ["plugins"],
    "Plugin": ["name", "description", "version", "review_pipeline", "tools"],
}

# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------

class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r})"


_TOKEN_RE = re.compile(
    r'(?P<LBRACE>\{)'
    r'|(?P<RBRACE>\})'
    r'|(?P<LPAREN>\()'
    r'|(?P<RPAREN>\))'
    r'|(?P<COLON>:)'
    r'|(?P<COMMA>,)'
    r'|(?P<STRING>"(?:[^"\\]|\\.)*")'
    r'|(?P<NUMBER>-?\d+(?:\.\d+)?)'
    r'|(?P<NAME>[_A-Za-z][_0-9A-Za-z]*)'
    r'|(?P<WS>\s+)'
    r'|(?P<COMMENT>#[^\n]*)'
)


def _tokenize(source: str) -> list[_Token]:
    tokens = []
    for m in _TOKEN_RE.finditer(source):
        kind = m.lastgroup
        if kind in ("WS", "COMMENT"):
            continue
        tokens.append(_Token(kind, m.group()))  # type: ignore[arg-type]
    return tokens


class ParseError(Exception):
    pass


class _Parser:
    """Parses a subset of GraphQL query syntax into an AST (plain dicts)."""

    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> _Token | None:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _consume(self, kind: str | None = None, value: str | None = None) -> _Token:
        tok = self._peek()
        if tok is None:
            raise ParseError("Unexpected end of input")
        if kind and tok.kind != kind:
            raise ParseError(f"Expected token kind {kind!r}, got {tok.kind!r} ({tok.value!r})")
        if value and tok.value != value:
            raise ParseError(f"Expected token value {value!r}, got {tok.value!r}")
        self._pos += 1
        return tok

    def parse_document(self) -> list[dict]:
        """Parse a GraphQL document (zero or more selection sets or operation defs)."""
        # Allow optional leading 'query' keyword
        if self._peek() and self._peek().kind == "NAME" and self._peek().value == "query":
            self._consume()
            # Optional operation name
            if self._peek() and self._peek().kind == "NAME":
                self._consume()
        return self.parse_selection_set()

    def parse_selection_set(self) -> list[dict]:
        self._consume("LBRACE")
        selections: list[dict] = []
        while self._peek() and self._peek().kind != "RBRACE":
            selections.append(self.parse_field())
            # optional comma
            if self._peek() and self._peek().kind == "COMMA":
                self._consume("COMMA")
        self._consume("RBRACE")
        return selections

    def parse_field(self) -> dict:
        """Parse one field, possibly with alias, arguments, and sub-selection."""
        name_tok = self._consume("NAME")
        alias: str | None = None
        name = name_tok.value

        # Alias check: NAME COLON NAME
        if self._peek() and self._peek().kind == "COLON":
            self._consume("COLON")
            alias = name
            name = self._consume("NAME").value

        # Arguments
        args: dict[str, Any] = {}
        if self._peek() and self._peek().kind == "LPAREN":
            args = self.parse_arguments()

        # Sub-selection
        sub: list[dict] = []
        if self._peek() and self._peek().kind == "LBRACE":
            sub = self.parse_selection_set()

        return {
            "name": name,
            "alias": alias,
            "args": args,
            "sub": sub,
        }

    def parse_arguments(self) -> dict[str, Any]:
        self._consume("LPAREN")
        args: dict[str, Any] = {}
        while self._peek() and self._peek().kind != "RPAREN":
            key = self._consume("NAME").value
            self._consume("COLON")
            val = self.parse_value()
            args[key] = val
            if self._peek() and self._peek().kind == "COMMA":
                self._consume("COMMA")
        self._consume("RPAREN")
        return args

    def parse_value(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise ParseError("Expected value, got end of input")
        if tok.kind == "STRING":
            self._consume()
            return tok.value[1:-1]  # strip surrounding quotes
        if tok.kind == "NUMBER":
            self._consume()
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "NAME":
            self._consume()
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return tok.value
        raise ParseError(f"Unexpected token in value position: {tok!r}")


def _parse(query: str) -> list[dict]:
    """Parse *query* into a list of field AST nodes. Raises ParseError on failure."""
    tokens = _tokenize(query)
    return _Parser(tokens).parse_document()


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def _resolve_health(_args: dict) -> dict:
    from backend.health_monitor import check_loop_health  # noqa: PLC0415
    import backend.module_health as _mh  # noqa: PLC0415
    loop = check_loop_health()
    modules = _mh.get_cached_module_health()
    return {
        "ok": loop.get("healthy", False),
        "loop": {
            "age": loop.get("age_seconds"),
            "threshold": loop.get("threshold_seconds"),
            "healthy": loop.get("healthy"),
        },
        "modules": [
            {"name": m.get("name"), "healthy": m.get("healthy"), "error": m.get("error")}
            for m in (modules.get("modules") or [])
        ],
    }


def _resolve_budget(_args: dict) -> dict:
    from backend.budget import BudgetTracker  # noqa: PLC0415
    bt = BudgetTracker()
    s = bt.get_status()
    return {
        "ceiling": s.get("ceiling"),
        "used": s.get("used"),
        "remaining": s.get("remaining"),
        "model": s.get("model"),
        "utilization_pct": s.get("pct_used"),
    }


def _resolve_cost(_args: dict) -> dict:
    from backend.cost_tracker import CostTracker  # noqa: PLC0415
    ct = CostTracker()
    s = ct.get_summary()
    return {
        "total_usd": s.get("total_usd"),
        "by_model": [
            {"model": m.get("model"), "cost": m.get("cost")}
            for m in (s.get("by_model") or [])
        ],
        "by_agent": [
            {"agent_id": a.get("agent_id"), "role": a.get("role"), "cost": a.get("cost")}
            for a in (s.get("by_agent") or [])
        ],
    }


def _resolve_registry(_args: dict) -> dict:
    from backend.registry import DiscussionRegistry  # noqa: PLC0415
    reg = DiscussionRegistry()
    data = reg.show()
    stats = reg.stats()
    discussions = []
    for d in data.get("discussions", []):
        discussions.append({
            "number": d.get("number"),
            "title": d.get("title"),
            "status": d.get("status"),
            "pr": d.get("pr"),
            "created_at": d.get("created_at"),
            "closed_at": d.get("closed_at"),
            "labels": d.get("labels", []),
        })
    return {
        "discussions": discussions,
        "stats": {
            "total": stats.get("total"),
            "open": stats.get("open"),
            "closed": stats.get("closed"),
            "velocity_7d": stats.get("velocity_7d"),
        },
    }


def _resolve_agents(_args: dict) -> dict:
    from backend.agent_cards import AgentCards  # noqa: PLC0415
    ac = AgentCards()
    names = ac.list_agents()
    agents = []
    for name in names:
        try:
            card = ac.get_card(name)
            agents.append({
                "role": card.get("role", name),
                "description": card.get("description", ""),
                "status": card.get("status", "active"),
                "tools": card.get("tools", []),
                "review_pipeline": card.get("review_pipeline", ""),
            })
        except Exception:  # noqa: BLE001
            agents.append({"role": name, "description": "", "status": "unknown", "tools": [], "review_pipeline": ""})
    return {"agents": agents}


def _resolve_kpi(_args: dict) -> dict:
    import time  # noqa: PLC0415
    import backend.kpi_engine as kpi_engine  # noqa: PLC0415
    try:
        data = kpi_engine.compute_all()
    except Exception:  # noqa: BLE001
        data = {}
    vel = data.get("velocity", {})
    cyc = data.get("pr_cycle_time", {})
    return {
        "velocity": {
            "prs_7d": vel.get("last_24h", 0),
            "prs_30d": vel.get("total_done", 0),
        },
        "cycle_time": {
            "median_hours": cyc.get("median_hours"),
            "p95_hours": cyc.get("p95_hours"),
        },
    }


def _resolve_control(_args: dict) -> dict:
    from backend.control_plane import ControlPlane  # noqa: PLC0415
    cp = ControlPlane()
    cp.load()
    gates_raw = cp.list_gates()
    gates = []
    if isinstance(gates_raw, dict):
        for k, v in gates_raw.items():
            gates.append({"key": k, "value": str(v)})
    elif isinstance(gates_raw, list):
        for item in gates_raw:
            gates.append({"key": item.get("key", ""), "value": str(item.get("value", ""))})
    return {"gates": gates}


def _resolve_audit(args: dict) -> list:
    from backend.audit_trail import get_audit_trail  # noqa: PLC0415
    at = get_audit_trail()
    limit = int(args.get("limit", 50))
    source = args.get("source")
    action = args.get("action")
    actor = args.get("actor")
    since = args.get("since")
    entries = at.query(source=source, action=action, actor=actor, since=since, limit=limit)
    result = []
    for e in entries:
        if isinstance(e, dict):
            result.append({
                "timestamp": e.get("timestamp"),
                "source": e.get("source"),
                "action": e.get("action"),
                "actor": e.get("actor"),
                "details": str(e.get("details", "")),
            })
    return result


def _resolve_replays(_args: dict) -> dict:
    from backend.replay import get_recorder  # noqa: PLC0415
    replays_raw = get_recorder().list_replays()
    replays = []
    for r in replays_raw:
        if isinstance(r, dict):
            replays.append({
                "agent_id": r.get("agent_id"),
                "role": r.get("role"),
                "discussion": str(r.get("discussion", "")),
                "started_at": r.get("started_at"),
                "duration_s": r.get("duration_s"),
                "event_count": r.get("event_count"),
            })
    return {"replays": replays}


def _resolve_spawn_queue(_args: dict) -> dict:
    from backend.spawn_queue import get_spawn_queue  # noqa: PLC0415
    sq = get_spawn_queue()
    status = sq.status()
    return {
        "pending_count": status.get("pending_count", 0),
        "active_count": status.get("active_count", 0),
        "utilization_pct": status.get("utilization_pct", 0.0),
    }


def _resolve_notifications(_args: dict) -> dict:
    from backend.notifier import get_notifier  # noqa: PLC0415
    records = get_notifier().get_history(50)
    return {"notifications": [str(r) for r in records]}


def _resolve_plugins(_args: dict) -> dict:
    from backend.plugin_loader import PluginLoader  # noqa: PLC0415
    pl = PluginLoader()
    names = pl.list_plugins()
    plugins = []
    for name in names:
        p = pl.get_plugin(name)
        if p:
            plugins.append({
                "name": p.name,
                "description": p.description,
                "version": p.version,
                "review_pipeline": p.review_pipeline,
                "tools": p.tools or [],
            })
    return {"plugins": plugins}


# Top-level resolver dispatch
_ROOT_RESOLVERS: dict[str, Any] = {
    "health": _resolve_health,
    "budget": _resolve_budget,
    "cost": _resolve_cost,
    "registry": _resolve_registry,
    "agents": _resolve_agents,
    "kpi": _resolve_kpi,
    "control": _resolve_control,
    "audit": _resolve_audit,
    "replays": _resolve_replays,
    "spawnQueue": _resolve_spawn_queue,
    "notifications": _resolve_notifications,
    "plugins": _resolve_plugins,
}

# ---------------------------------------------------------------------------
# Introspection resolvers
# ---------------------------------------------------------------------------

def _introspect_schema(sub_fields: list[dict]) -> dict:
    """Handle __schema introspection."""
    result: dict = {}
    for f in sub_fields:
        if f["name"] == "types":
            type_list = []
            for type_name, fields in _TYPE_FIELDS.items():
                type_entry: dict = {}
                for tf in f["sub"]:
                    if tf["name"] == "name":
                        type_entry["name"] = type_name
                    elif tf["name"] == "fields":
                        field_entries = []
                        for field_name in fields:
                            fe: dict = {}
                            for ff in tf["sub"]:
                                if ff["name"] == "name":
                                    fe["name"] = field_name
                                elif ff["name"] == "type":
                                    fe["type"] = _field_type_info(ff["sub"])
                            field_entries.append(fe)
                        type_entry["fields"] = field_entries
                type_list.append(type_entry)
            result["types"] = type_list
    return result


def _introspect_type(type_name: str, sub_fields: list[dict]) -> dict | None:
    """Handle __type(name:...) introspection."""
    if type_name not in _TYPE_FIELDS:
        return None
    result: dict = {}
    for f in sub_fields:
        if f["name"] == "name":
            result["name"] = type_name
        elif f["name"] == "fields":
            field_entries = []
            for field_name in _TYPE_FIELDS[type_name]:
                fe: dict = {}
                for ff in f["sub"]:
                    if ff["name"] == "name":
                        fe["name"] = field_name
                    elif ff["name"] == "type":
                        fe["type"] = _field_type_info(ff["sub"])
                field_entries.append(fe)
            result["fields"] = field_entries
    return result


def _field_type_info(sub_fields: list[dict]) -> dict:
    """Build a type info dict for introspection."""
    result: dict = {}
    for f in sub_fields:
        if f["name"] == "name":
            result["name"] = "String"  # simplified — all fields report String
    return result


# ---------------------------------------------------------------------------
# Executor — walks AST and calls resolvers
# ---------------------------------------------------------------------------

def _filter_object(data: Any, sub_fields: list[dict], errors: list[dict], path: str) -> Any:
    """Recursively filter *data* to only the fields requested in *sub_fields*."""
    if not sub_fields:
        # Leaf node — return data as-is
        return data

    if isinstance(data, list):
        return [_filter_object(item, sub_fields, errors, f"{path}[{i}]") for i, item in enumerate(data)]

    if not isinstance(data, dict):
        return data

    result: dict = {}
    for field in sub_fields:
        field_name = field["name"]
        alias = field["alias"] or field_name
        output_key = alias

        if field_name not in data:
            errors.append({
                "message": f"Field '{field_name}' does not exist at path '{path}'",
                "path": f"{path}.{field_name}",
            })
            result[output_key] = None
            continue

        value = data[field_name]
        result[output_key] = _filter_object(value, field["sub"], errors, f"{path}.{field_name}")

    return result


def _execute_selections(selections: list[dict], errors: list[dict]) -> dict:
    """Walk top-level selections and call resolvers."""
    data: dict = {}

    for field in selections:
        field_name = field["name"]
        alias = field["alias"] or field_name
        args = field["args"]
        sub = field["sub"]

        # Introspection — __schema
        if field_name == "__schema":
            try:
                data[alias] = _introspect_schema(sub)
            except Exception as exc:  # noqa: BLE001
                errors.append({"message": f"__schema introspection error: {exc}", "path": field_name})
                data[alias] = None
            continue

        # Introspection — __type
        if field_name == "__type":
            type_name = args.get("name", "")
            try:
                data[alias] = _introspect_type(type_name, sub)
            except Exception as exc:  # noqa: BLE001
                errors.append({"message": f"__type introspection error: {exc}", "path": field_name})
                data[alias] = None
            continue

        resolver = _ROOT_RESOLVERS.get(field_name)
        if resolver is None:
            errors.append({
                "message": f"Unknown field '{field_name}' on type 'Query'",
                "path": field_name,
            })
            data[alias] = None
            continue

        try:
            raw = resolver(args)
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "message": f"Resolver error for field '{field_name}': {exc}",
                "path": field_name,
            })
            data[alias] = None
            continue

        data[alias] = _filter_object(raw, sub, errors, field_name)

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute(query: str) -> dict:
    """Execute a GraphQL query string and return a response dict.

    Always returns {"data": {...}} on success. On parse or resolver errors,
    returns {"errors": [...]} or {"data": {...}, "errors": [...]} per the
    GraphQL specification (errors alongside partial data when possible).
    """
    try:
        selections = _parse(query)
    except ParseError as exc:
        return {"errors": [{"message": f"Parse error: {exc}"}]}

    errors: list[dict] = []
    try:
        data = _execute_selections(selections, errors)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [{"message": f"Execution error: {exc}"}]}

    if errors:
        return {"data": data, "errors": errors}
    return {"data": data}


def get_schema_types() -> list[dict]:
    """Return a list of type name + field name dicts for introspection."""
    return [
        {"name": name, "fields": [{"name": f} for f in fields]}
        for name, fields in _TYPE_FIELDS.items()
    ]
