"""
Python client SDK for the autonomous-forever REST API.

Wraps all API endpoints in typed methods with automatic auth, error handling,
and response dataclasses. Uses only stdlib — no requests, no httpx.

Quick start:

    from backend.sdk import AutonomousClient

    # Token from argument or AF_API_TOKEN env var
    client = AutonomousClient("http://localhost:18099", token="my-token")
    status = client.health()
    print(status.ok)  # True

    # Context manager
    with AutonomousClient("http://localhost:18099") as c:
        reg = c.registry()
        for d in reg.discussions:
            print(d.number, d.title)

    # Audit with query params
    entries = client.audit(source="api", limit=5)

Error handling:

    from backend.sdk import APIError
    try:
        client.agent("nonexistent")
    except APIError as e:
        print(e.status_code, e.message)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class APIError(Exception):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, response_body: str = "") -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.response_body = response_body

    def __repr__(self) -> str:
        return (
            f"APIError(status_code={self.status_code!r}, "
            f"message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Response dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HealthStatus:
    ok: bool
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "HealthStatus":
        ok = d.pop("ok", False)
        return cls(ok=ok, extra=d)

    def __repr__(self) -> str:
        return f"HealthStatus(ok={self.ok!r})"


@dataclass
class LoopHealth:
    healthy: bool
    age_seconds: Optional[float] = None
    threshold_seconds: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "LoopHealth":
        healthy = d.pop("healthy", False)
        age = d.pop("age_seconds", None)
        threshold = d.pop("threshold_seconds", None)
        return cls(healthy=healthy, age_seconds=age, threshold_seconds=threshold, extra=d)

    def __repr__(self) -> str:
        return f"LoopHealth(healthy={self.healthy!r}, age_seconds={self.age_seconds!r})"


@dataclass
class ModuleHealth:
    name: str
    ok: bool
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ModuleHealth":
        name = d.pop("name", "")
        ok = d.pop("ok", False)
        error = d.pop("error", None)
        return cls(name=name, ok=ok, error=error, extra=d)

    def __repr__(self) -> str:
        return f"ModuleHealth(name={self.name!r}, ok={self.ok!r})"


@dataclass
class BudgetStatus:
    ceiling: Optional[int] = None
    spent: Optional[float] = None
    remaining: Optional[float] = None
    model: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetStatus":
        ceiling = d.pop("ceiling", None)
        spent = d.pop("spent", None)
        remaining = d.pop("remaining", None)
        model = d.pop("model", None)
        return cls(ceiling=ceiling, spent=spent, remaining=remaining, model=model, extra=d)

    def __repr__(self) -> str:
        return (
            f"BudgetStatus(ceiling={self.ceiling!r}, spent={self.spent!r}, "
            f"remaining={self.remaining!r})"
        )


@dataclass
class CostBreakdown:
    session_total: Optional[float] = None
    per_agent: dict = field(default_factory=dict)
    per_discussion: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "CostBreakdown":
        session_total = d.pop("session_total", None)
        per_agent = d.pop("per_agent", {})
        per_discussion = d.pop("per_discussion", {})
        return cls(session_total=session_total, per_agent=per_agent,
                   per_discussion=per_discussion, extra=d)

    def __repr__(self) -> str:
        return f"CostBreakdown(session_total={self.session_total!r})"


@dataclass
class CostSummary:
    total: Optional[float] = None
    model_breakdown: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "CostSummary":
        total = d.pop("total", None)
        model_breakdown = d.pop("model_breakdown", {})
        return cls(total=total, model_breakdown=model_breakdown, extra=d)

    def __repr__(self) -> str:
        return f"CostSummary(total={self.total!r})"


@dataclass
class Discussion:
    number: Optional[int] = None
    title: Optional[str] = None
    status: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Discussion":
        number = d.pop("number", None)
        title = d.pop("title", None)
        status = d.pop("status", None)
        return cls(number=number, title=title, status=status, extra=d)

    def __repr__(self) -> str:
        return f"Discussion(number={self.number!r}, title={self.title!r})"


@dataclass
class Registry:
    discussions: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Registry":
        raw_discussions = d.pop("discussions", [])
        discussions = [
            Discussion.from_dict(dict(item)) if isinstance(item, dict) else item
            for item in raw_discussions
        ]
        return cls(discussions=discussions, extra=d)

    def __repr__(self) -> str:
        return f"Registry(discussions={len(self.discussions)} items)"


@dataclass
class RegistryStats:
    total: Optional[int] = None
    by_status: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "RegistryStats":
        total = d.pop("total", None)
        by_status = d.pop("by_status", {})
        return cls(total=total, by_status=by_status, extra=d)

    def __repr__(self) -> str:
        return f"RegistryStats(total={self.total!r})"


@dataclass
class ControlPlane:
    gates: list = field(default_factory=list)
    policies: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ControlPlane":
        gates = d.pop("gates", [])
        policies = d.pop("policies", {})
        return cls(gates=gates, policies=policies, extra=d)

    def __repr__(self) -> str:
        return f"ControlPlane(gates={len(self.gates)} gates)"


@dataclass
class Gate:
    name: str
    enabled: bool
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Gate":
        name = d.pop("name", "")
        enabled = d.pop("enabled", False)
        return cls(name=name, enabled=enabled, extra=d)

    def __repr__(self) -> str:
        return f"Gate(name={self.name!r}, enabled={self.enabled!r})"


@dataclass
class AuditEntry:
    source: Optional[str] = None
    action: Optional[str] = None
    actor: Optional[str] = None
    timestamp: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEntry":
        source = d.pop("source", None)
        action = d.pop("action", None)
        actor = d.pop("actor", None)
        timestamp = d.pop("timestamp", None)
        return cls(source=source, action=action, actor=actor, timestamp=timestamp, extra=d)

    def __repr__(self) -> str:
        return (
            f"AuditEntry(source={self.source!r}, action={self.action!r}, "
            f"actor={self.actor!r})"
        )


@dataclass
class AuditStats:
    by_source: dict = field(default_factory=dict)
    by_action: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditStats":
        by_source = d.pop("by_source", {})
        by_action = d.pop("by_action", {})
        return cls(by_source=by_source, by_action=by_action, extra=d)

    def __repr__(self) -> str:
        return f"AuditStats(by_source={self.by_source!r})"


@dataclass
class AgentCard:
    role: Optional[str] = None
    description: Optional[str] = None
    capabilities: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCard":
        role = d.pop("role", None)
        description = d.pop("description", None)
        capabilities = d.pop("capabilities", [])
        return cls(role=role, description=description, capabilities=capabilities, extra=d)

    def __repr__(self) -> str:
        return f"AgentCard(role={self.role!r})"


@dataclass
class KPISnapshot:
    velocity: Optional[dict] = None
    cycle_time: Optional[dict] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "KPISnapshot":
        velocity = d.pop("velocity", None)
        cycle_time = d.pop("cycle_time", None)
        return cls(velocity=velocity, cycle_time=cycle_time, extra=d)

    def __repr__(self) -> str:
        return "KPISnapshot()"


@dataclass
class Velocity:
    prs_per_day: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Velocity":
        prs_per_day = d.pop("prs_per_day", None)
        return cls(prs_per_day=prs_per_day, extra=d)

    def __repr__(self) -> str:
        return f"Velocity(prs_per_day={self.prs_per_day!r})"


@dataclass
class CycleTime:
    median_hours: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "CycleTime":
        median_hours = d.pop("median_hours", None)
        return cls(median_hours=median_hours, extra=d)

    def __repr__(self) -> str:
        return f"CycleTime(median_hours={self.median_hours!r})"


@dataclass
class ReplayMeta:
    agent_id: Optional[str] = None
    role: Optional[str] = None
    started_at: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayMeta":
        agent_id = d.pop("agent_id", None)
        role = d.pop("role", None)
        started_at = d.pop("started_at", None)
        return cls(agent_id=agent_id, role=role, started_at=started_at, extra=d)

    def __repr__(self) -> str:
        return f"ReplayMeta(agent_id={self.agent_id!r}, role={self.role!r})"


@dataclass
class ReplayEvent:
    event_type: Optional[str] = None
    timestamp: Optional[str] = None
    content: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayEvent":
        event_type = d.pop("event_type", None)
        timestamp = d.pop("timestamp", None)
        content = d.pop("content", None)
        return cls(event_type=event_type, timestamp=timestamp, content=content, extra=d)

    def __repr__(self) -> str:
        return f"ReplayEvent(event_type={self.event_type!r})"


@dataclass
class ReplaySummary:
    agent_id: Optional[str] = None
    role: Optional[str] = None
    event_count: Optional[int] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ReplaySummary":
        agent_id = d.pop("agent_id", None)
        role = d.pop("role", None)
        event_count = d.pop("event_count", None)
        return cls(agent_id=agent_id, role=role, event_count=event_count, extra=d)

    def __repr__(self) -> str:
        return f"ReplaySummary(agent_id={self.agent_id!r}, event_count={self.event_count!r})"


@dataclass
class SpawnQueueStatus:
    pending_count: Optional[int] = None
    active_count: Optional[int] = None
    utilization_pct: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SpawnQueueStatus":
        pending_count = d.pop("pending_count", None)
        active_count = d.pop("active_count", None)
        utilization_pct = d.pop("utilization_pct", None)
        return cls(pending_count=pending_count, active_count=active_count,
                   utilization_pct=utilization_pct, extra=d)

    def __repr__(self) -> str:
        return (
            f"SpawnQueueStatus(pending_count={self.pending_count!r}, "
            f"active_count={self.active_count!r})"
        )


@dataclass
class SpawnRequest:
    role: Optional[str] = None
    prompt: Optional[str] = None
    discussion: Optional[int] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "SpawnRequest":
        role = d.pop("role", None)
        prompt = d.pop("prompt", None)
        discussion = d.pop("discussion", None)
        return cls(role=role, prompt=prompt, discussion=discussion, extra=d)

    def __repr__(self) -> str:
        return f"SpawnRequest(role={self.role!r}, discussion={self.discussion!r})"


@dataclass
class ActiveAgent:
    agent_id: Optional[str] = None
    role: Optional[str] = None
    started_at: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ActiveAgent":
        agent_id = d.pop("agent_id", None)
        role = d.pop("role", None)
        started_at = d.pop("started_at", None)
        return cls(agent_id=agent_id, role=role, started_at=started_at, extra=d)

    def __repr__(self) -> str:
        return f"ActiveAgent(agent_id={self.agent_id!r}, role={self.role!r})"


@dataclass
class Notification:
    channel: Optional[str] = None
    message: Optional[str] = None
    sent_at: Optional[str] = None
    success: Optional[bool] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Notification":
        channel = d.pop("channel", None)
        message = d.pop("message", None)
        sent_at = d.pop("sent_at", None)
        success = d.pop("success", None)
        return cls(channel=channel, message=message, sent_at=sent_at, success=success, extra=d)

    def __repr__(self) -> str:
        return f"Notification(channel={self.channel!r}, success={self.success!r})"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AutonomousClient:
    """
    Client for the autonomous-forever REST API.

    Args:
        base_url: Base URL of the running API server (default: http://localhost:18099).
        token: Bearer token for auth. If None, reads AF_API_TOKEN env var.
        timeout_connect: Connection timeout in seconds (default: 10).
        timeout_read: Read timeout in seconds (default: 30).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:18099",
        token: Optional[str] = None,
        timeout_connect: float = 10,
        timeout_read: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("AF_API_TOKEN")
        self.timeout_connect = timeout_connect
        self.timeout_read = timeout_read

    def __enter__(self) -> "AutonomousClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass  # no persistent connection to close

    def __repr__(self) -> str:
        return f"AutonomousClient(base_url={self.base_url!r})"

    # -----------------------------------------------------------------------
    # Internal HTTP helper
    # -----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """
        Send an HTTP request and return the parsed JSON response.

        Raises APIError on non-2xx status codes.
        """
        url = self.base_url + path
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = url + "?" + urllib.parse.urlencode(filtered)

        headers: dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_read
            ) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw_body = ""
            try:
                raw_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                parsed = json.loads(raw_body)
                message = parsed.get("error") or parsed.get("message") or raw_body
            except Exception:
                message = raw_body or exc.reason or str(exc)
            raise APIError(
                status_code=exc.code,
                message=message,
                response_body=raw_body,
            ) from exc

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    def health(self) -> HealthStatus:
        """GET /health — overall server health."""
        return HealthStatus.from_dict(dict(self._request("GET", "/health")))

    def health_loop(self) -> LoopHealth:
        """GET /health/loop — loop heartbeat health."""
        return LoopHealth.from_dict(dict(self._request("GET", "/health/loop")))

    def health_modules(self) -> list[ModuleHealth]:
        """GET /health/modules — per-module import health."""
        data = self._request("GET", "/health/modules")
        if isinstance(data, list):
            return [ModuleHealth.from_dict(dict(item)) for item in data]
        return [ModuleHealth.from_dict(dict(data))]

    # -----------------------------------------------------------------------
    # Budget and cost
    # -----------------------------------------------------------------------

    def budget_status(self) -> BudgetStatus:
        """GET /budget/status — current budget snapshot."""
        return BudgetStatus.from_dict(dict(self._request("GET", "/budget/status")))

    def budget_init(self, ceiling: int, model: str) -> dict:
        """POST /budget/init — init or reset the session budget."""
        return self._request("POST", "/budget/init", body={"ceiling": ceiling, "model": model})

    def cost(self) -> CostBreakdown:
        """GET /cost — full cost breakdown."""
        return CostBreakdown.from_dict(dict(self._request("GET", "/cost")))

    def cost_summary(self) -> CostSummary:
        """GET /cost/summary — lightweight total + model breakdown."""
        return CostSummary.from_dict(dict(self._request("GET", "/cost/summary")))

    # -----------------------------------------------------------------------
    # Registry
    # -----------------------------------------------------------------------

    def registry(self) -> Registry:
        """GET /registry — full registry with discussions."""
        return Registry.from_dict(dict(self._request("GET", "/registry")))

    def registry_stats(self) -> RegistryStats:
        """GET /registry/stats — velocity stats only."""
        return RegistryStats.from_dict(dict(self._request("GET", "/registry/stats")))

    # -----------------------------------------------------------------------
    # Control plane
    # -----------------------------------------------------------------------

    def control(self) -> ControlPlane:
        """GET /control — gates and policies."""
        return ControlPlane.from_dict(dict(self._request("GET", "/control")))

    def control_gates(self) -> list[Gate]:
        """GET /control/gates — list of gates."""
        data = self._request("GET", "/control/gates")
        if isinstance(data, list):
            return [Gate.from_dict(dict(item)) for item in data]
        return [Gate.from_dict(dict(data))]

    def control_set(self, key: str, value: Any) -> dict:
        """POST /control/set — set a control key."""
        return self._request("POST", "/control/set", body={"key": key, "value": value})

    def control_audit(self) -> list[AuditEntry]:
        """GET /control/audit — control plane audit log."""
        data = self._request("GET", "/control/audit")
        if isinstance(data, list):
            return [AuditEntry.from_dict(dict(item)) for item in data]
        return []

    # -----------------------------------------------------------------------
    # Audit
    # -----------------------------------------------------------------------

    def audit(
        self,
        source: Optional[str] = None,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[AuditEntry]:
        """GET /audit — filtered audit trail."""
        params = {"source": source, "action": action, "actor": actor,
                  "since": since, "limit": limit}
        data = self._request("GET", "/audit", params=params)
        if isinstance(data, list):
            return [AuditEntry.from_dict(dict(item)) for item in data]
        return []

    def audit_stats(self) -> AuditStats:
        """GET /audit/stats — counts by source and action."""
        return AuditStats.from_dict(dict(self._request("GET", "/audit/stats")))

    # -----------------------------------------------------------------------
    # Agents
    # -----------------------------------------------------------------------

    def agents(self) -> list[str]:
        """GET /agents — list of agent role names."""
        data = self._request("GET", "/agents")
        if isinstance(data, list):
            return data
        return []

    def agent(self, role: str) -> AgentCard:
        """GET /agents/<role> — card for a specific role. Raises APIError(404) if not found."""
        return AgentCard.from_dict(dict(self._request("GET", f"/agents/{role}")))

    # -----------------------------------------------------------------------
    # KPI
    # -----------------------------------------------------------------------

    def kpi(self) -> KPISnapshot:
        """GET /kpi — full KPI snapshot."""
        return KPISnapshot.from_dict(dict(self._request("GET", "/kpi")))

    def kpi_velocity(self) -> Velocity:
        """GET /kpi/velocity — velocity subsection."""
        return Velocity.from_dict(dict(self._request("GET", "/kpi/velocity")))

    def kpi_cycle_time(self) -> CycleTime:
        """GET /kpi/cycle-time — PR cycle time subsection."""
        return CycleTime.from_dict(dict(self._request("GET", "/kpi/cycle-time")))

    # -----------------------------------------------------------------------
    # Replays
    # -----------------------------------------------------------------------

    def replays(self, limit: int = 20) -> list[ReplayMeta]:
        """GET /replays — list recent replay metadata."""
        data = self._request("GET", "/replays", params={"limit": limit})
        if isinstance(data, list):
            return [ReplayMeta.from_dict(dict(item)) for item in data]
        return []

    def replay(self, agent_id: str) -> list[ReplayEvent]:
        """GET /replays/<agent_id> — full event list for one agent run."""
        data = self._request("GET", f"/replays/{agent_id}")
        if isinstance(data, list):
            return [ReplayEvent.from_dict(dict(item)) for item in data]
        return []

    def replay_summary(self, agent_id: str) -> ReplaySummary:
        """GET /replays/<agent_id>/summary — header + footer only."""
        return ReplaySummary.from_dict(
            dict(self._request("GET", f"/replays/{agent_id}/summary"))
        )

    # -----------------------------------------------------------------------
    # Spawn queue
    # -----------------------------------------------------------------------

    def spawn_queue(self) -> SpawnQueueStatus:
        """GET /spawn-queue — queue status."""
        return SpawnQueueStatus.from_dict(dict(self._request("GET", "/spawn-queue")))

    def spawn_queue_pending(self) -> list[SpawnRequest]:
        """GET /spawn-queue/pending — list pending spawn requests."""
        data = self._request("GET", "/spawn-queue/pending")
        if isinstance(data, list):
            return [SpawnRequest.from_dict(dict(item)) for item in data]
        return []

    def spawn_queue_active(self) -> list[ActiveAgent]:
        """GET /spawn-queue/active — list active agents."""
        data = self._request("GET", "/spawn-queue/active")
        if isinstance(data, list):
            return [ActiveAgent.from_dict(dict(item)) for item in data]
        return []

    def spawn_enqueue(
        self,
        role: str,
        prompt: str,
        discussion: Optional[int] = None,
    ) -> dict:
        """POST /spawn-queue/enqueue — enqueue a spawn request."""
        body: dict[str, Any] = {"role": role, "prompt": prompt}
        if discussion is not None:
            body["discussion"] = discussion
        return self._request("POST", "/spawn-queue/enqueue", body=body)

    # -----------------------------------------------------------------------
    # Notifications
    # -----------------------------------------------------------------------

    def notifications_history(self) -> list[Notification]:
        """GET /notifications/history — last 50 notification dispatch records."""
        data = self._request("GET", "/notifications/history")
        if isinstance(data, list):
            return [Notification.from_dict(dict(item)) for item in data]
        return []

    def notifications_test(self) -> dict:
        """POST /notifications/test — send test notification to all channels."""
        return self._request("POST", "/notifications/test")
