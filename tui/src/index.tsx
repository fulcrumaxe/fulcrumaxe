/**
 * fulcrumaxe TUI
 * TypeScript/ink wrapper around the backend/server.py Python backend.
 *
 * Spawns backend/server.py as a child process, wires events into AgentFeed.
 * Supports multi-agent tab switching via Ctrl+Left/Right (or Ctrl+1-9).
 */
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { render, Text, Box, useInput } from 'ink';
import { exec } from 'child_process';
import { promisify } from 'util';
import { readFileSync } from 'fs';
import * as path from 'path';
import { ErrorBoundary } from './ErrorBoundary.js';
import { BackendClient } from './backend.js';
import { AgentFeed } from './AgentFeed.js';
import { AgentInfo, TabBar } from './TabBar.js';
import { ChatInput } from './ChatInput.js';
import { StatusBar } from './StatusBar.js';
import { BackendEvent, AgentSpawnEvent, AgentEventEnvelope, AgentExitEvent, AgentFeedFileEvent } from './types.js';
import { startFeedWatcher, FeedEvent } from './feedWatcher.js';

const execAsync = promisify(exec);

interface CoordinationState {
  budgetPct: number | null;
  budgetWarn: boolean;
  queueCounts: { active: number; ready: number } | null;
  loopAgo: string | null;
}

async function readBudgetAsync(): Promise<{ budgetPct: number | null; budgetWarn: boolean }> {
  try {
    const { stdout } = await execAsync('python3 backend/budget.py status', {
      timeout: 5000,
      cwd: process.cwd(),
    });
    const data = JSON.parse(stdout) as { spent?: number; ceiling?: number };
    if (data.spent == null || data.ceiling == null || data.ceiling === 0) {
      return { budgetPct: null, budgetWarn: false };
    }
    const pct = Math.round((data.spent / data.ceiling) * 100);
    return { budgetPct: pct, budgetWarn: pct >= 60 };
  } catch {
    return { budgetPct: null, budgetWarn: false };
  }
}

function readLoopAgo(): string | null {
  try {
    const metricsPath = `${process.cwd()}/.autonomous-team/loop-metrics.jsonl`;
    const content = readFileSync(metricsPath, 'utf8').trim();
    if (!content) return null;
    const lines = content.split('\n');
    const lastLine = lines[lines.length - 1];
    if (!lastLine) return null;
    const data = JSON.parse(lastLine) as { timestamp?: string; duration_seconds?: number };
    if (!data.timestamp) return null;
    const ago = Math.floor((Date.now() - new Date(data.timestamp).getTime()) / 1000);
    const agoMin = Math.floor(ago / 60);
    const agoSec = ago % 60;
    const agoLabel = agoMin > 0 ? `${agoMin}m ${agoSec}s ago` : `${agoSec}s ago`;
    const durationLabel = data.duration_seconds != null
      ? ` (${data.duration_seconds}s)`
      : '';
    return `${agoLabel}${durationLabel}`;
  } catch {
    return null;
  }
}

async function readQueueCountsAsync(): Promise<{ active: number; ready: number } | null> {
  try {
    const { stdout } = await execAsync(
      `gh api graphql --repo autonomous-agent-7/autonomous-forever -f query='query { repository(owner:"autonomous-agent-7", name:"autonomous-forever") { discussions(first:50, states:[OPEN]) { nodes { body } } } }'`,
      { timeout: 15000 }
    );
    const result = JSON.parse(stdout) as {
      data?: { repository?: { discussions?: { nodes?: Array<{ body: string }> } } };
    };
    const nodes = result.data?.repository?.discussions?.nodes ?? [];
    let active = 0;
    let ready = 0;
    for (const node of nodes) {
      if (/STATUS:SPEC_READY/.test(node.body)) {
        ready++;
      } else if (/STATUS:(DISCUSSING|IMPLEMENTING|REVIEWING)/.test(node.body)) {
        active++;
      }
    }
    return { active, ready };
  } catch {
    return null;
  }
}

function useCoordinationState(): CoordinationState {
  const [state, setState] = useState<CoordinationState>({
    budgetPct: null,
    budgetWarn: false,
    queueCounts: null,
    loopAgo: null,
  });

  useEffect(() => {
    // Initial load.
    const fast = () => {
      const loopAgo = readLoopAgo();
      setState((prev) => ({ ...prev, loopAgo }));
      void readBudgetAsync().then(({ budgetPct, budgetWarn }) => {
        setState((prev) => ({ ...prev, budgetPct, budgetWarn }));
      });
    };
    const slow = () => {
      void readQueueCountsAsync().then((queueCounts) => {
        setState((prev) => ({ ...prev, queueCounts }));
      });
    };

    fast();
    slow();

    const fastInterval = setInterval(fast, 30_000);
    const slowInterval = setInterval(slow, 5 * 60_000);

    return () => {
      clearInterval(fastInterval);
      clearInterval(slowInterval);
    };
  }, []);

  return state;
}

/** Build an interleaved event list from all agents, preserving order by array position. */
function buildAllEvents(agents: Map<string, AgentInfo>): BackendEvent[] {
  // Simple concat in agent insertion order — events within each agent are
  // already in arrival order. For the unified view we interleave by appending
  // each agent's events in turn, which is good enough given they all stream
  // asynchronously in real usage.
  const result: BackendEvent[] = [];
  for (const info of agents.values()) {
    for (const ev of info.events) {
      result.push(ev);
    }
  }
  return result;
}

function App() {
  // Global flat event list for backward-compat path (all go to agent-0).
  const [agents, setAgents] = useState<Map<string, AgentInfo>>(() => {
    const m = new Map<string, AgentInfo>();
    m.set('agent-0', {
      id: 'agent-0',
      name: 'Team Lead',
      parentId: null,
      status: 'running',
      events: [],
    });
    return m;
  });

  const [activeTab, setActiveTab] = useState<string>('all');
  const [isConnected, setIsConnected] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [tokenUsage, setTokenUsage] = useState({ input: 0, output: 0 });
  const [reconnectAttempt, setReconnectAttempt] = useState<number | undefined>(undefined);
  const [fatalMessage, setFatalMessage] = useState<string | undefined>(undefined);
  // Per-agent token totals for the StatusBar.
  const [agentTokens, setAgentTokens] = useState<Map<string, { input: number; output: number }>>(
    () => new Map([['agent-0', { input: 0, output: 0 }]])
  );

  const coordination = useCoordinationState();

  const clientRef = useRef<BackendClient | null>(null);

  // Helper: append an event to a specific agent's events array.
  const appendToAgent = useCallback((agentId: string, event: BackendEvent) => {
    setAgents((prev) => {
      const next = new Map(prev);
      const info = next.get(agentId);
      if (info) {
        next.set(agentId, { ...info, events: [...info.events, event] });
      }
      return next;
    });
  }, []);

  useEffect(() => {
    const client = new BackendClient();
    clientRef.current = client;

    client.on('event', (event: BackendEvent) => {
      if (event.type === 'agent_spawn') {
        const e = event as AgentSpawnEvent;
        setAgents((prev) => {
          if (prev.has(e.agent_id)) return prev;
          const next = new Map(prev);
          next.set(e.agent_id, {
            id: e.agent_id,
            name: e.agent_name,
            parentId: e.parent_id,
            status: 'running',
            events: [],
          });
          return next;
        });
        setAgentTokens((prev) => {
          if (prev.has(e.agent_id)) return prev;
          const next = new Map(prev);
          next.set(e.agent_id, { input: 0, output: 0 });
          return next;
        });
        return;
      }

      if (event.type === 'agent_event') {
        const e = event as AgentEventEnvelope;
        appendToAgent(e.agent_id, e.inner);
        if (e.inner.type === 'usage') {
          const usageInner = e.inner;
          setAgentTokens((prev) => {
            const next = new Map(prev);
            const cur = next.get(e.agent_id) ?? { input: 0, output: 0 };
            next.set(e.agent_id, {
              input: cur.input + usageInner.usage.input_tokens,
              output: cur.output + usageInner.usage.output_tokens,
            });
            return next;
          });
          setTokenUsage((prev) => ({
            input: prev.input + usageInner.usage.input_tokens,
            output: prev.output + usageInner.usage.output_tokens,
          }));
        }
        if (e.inner.type === 'done' || e.inner.type === 'error') {
          // Don't set isLoading=false on sub-agent done — only top-level does that.
        }
        return;
      }

      if (event.type === 'agent_exit') {
        const e = event as AgentExitEvent;
        setAgents((prev) => {
          const next = new Map(prev);
          const info = next.get(e.agent_id);
          if (info) {
            next.set(e.agent_id, {
              ...info,
              status: e.exit_code === 0 || e.exit_code === null ? 'done' : 'error',
            });
          }
          return next;
        });
        return;
      }

      // Unwrapped event — goes to agent-0 (backward compat).
      appendToAgent('agent-0', event);

      if (event.type === 'usage') {
        setAgentTokens((prev) => {
          const next = new Map(prev);
          const cur = next.get('agent-0') ?? { input: 0, output: 0 };
          next.set('agent-0', {
            input: cur.input + event.usage.input_tokens,
            output: cur.output + event.usage.output_tokens,
          });
          return next;
        });
        setTokenUsage((prev) => ({
          input: prev.input + event.usage.input_tokens,
          output: prev.output + event.usage.output_tokens,
        }));
      }

      if (event.type === 'done' || event.type === 'error') {
        setIsLoading(false);
        // Mark agent-0 done.
        setAgents((prev) => {
          const next = new Map(prev);
          const info = next.get('agent-0');
          if (info) {
            next.set('agent-0', {
              ...info,
              status: event.type === 'done' ? 'done' : 'error',
            });
          }
          return next;
        });
      }
    });

    client.on('ready', () => {
      setIsConnected(true);
    });

    client.on('reconnecting', ({ attempt, maxRetries }: { attempt: number; maxRetries: number }) => {
      setIsConnected(false);
      setIsLoading(false);
      setReconnectAttempt(attempt);
      appendToAgent('agent-0', {
        id: 'local',
        type: 'error',
        error: `Backend disconnected — reconnecting (attempt ${attempt}/${maxRetries})...`,
      } as BackendEvent);
    });

    client.on('reconnected', () => {
      setReconnectAttempt(undefined);
      setFatalMessage(undefined);
    });

    client.on('fatal', (err: Error) => {
      setIsConnected(false);
      setIsLoading(false);
      setReconnectAttempt(undefined);
      setFatalMessage(err.message);
      appendToAgent('agent-0', { id: 'local', type: 'error', error: err.message } as BackendEvent);
    });

    client.on('exit', () => {
      setIsConnected(false);
      setIsLoading(false);
    });

    client.on('error', (err: Error) => {
      setIsConnected(false);
      setIsLoading(false);
      appendToAgent('agent-0', { id: 'local', type: 'error', error: err.message } as BackendEvent);
    });

    client.start();

    return () => {
      client.stop();
      clientRef.current = null;
    };
  }, [appendToAgent]);

  // Feed watcher: tail agent-feed.jsonl and inject events into the agents map.
  useEffect(() => {
    const feedPath = path.join(process.cwd(), '.autonomous-team', 'agent-feed.jsonl');

    const handleFeedEvents = (feedEvents: FeedEvent[]) => {
      for (const fe of feedEvents) {
        const agentId = fe.agent;

        // Ensure an agent entry exists for this feed agent.
        setAgents((prev) => {
          if (prev.has(agentId)) return prev;
          const next = new Map(prev);
          next.set(agentId, {
            id: agentId,
            name: fe.role,
            parentId: null,
            status: 'running',
            events: [],
          });
          return next;
        });

        // Build a BackendEvent from the feed line and append it.
        const feedBackendEvent: AgentFeedFileEvent = {
          type: 'agent_feed',
          agent: fe.agent,
          role: fe.role,
          event: fe.event,
          detail: fe.detail,
          discussion: fe.discussion,
        };
        appendToAgent(agentId, feedBackendEvent);
      }
    };

    const stopWatcher = startFeedWatcher(feedPath, handleFeedEvents);
    return stopWatcher;
  }, [appendToAgent]);

  // Keyboard: Ctrl+Left / Ctrl+Right to cycle tabs; Ctrl+1-9 to jump.
  useInput((_input, key) => {
    const tabIds = ['all', ...Array.from(agents.keys())];

    if (key.ctrl && key.leftArrow) {
      setActiveTab((cur) => {
        const idx = tabIds.indexOf(cur);
        const prev = (idx - 1 + tabIds.length) % tabIds.length;
        return tabIds[prev] ?? 'all';
      });
      return;
    }

    if (key.ctrl && key.rightArrow) {
      setActiveTab((cur) => {
        const idx = tabIds.indexOf(cur);
        const next = (idx + 1) % tabIds.length;
        return tabIds[next] ?? 'all';
      });
      return;
    }

    // Ctrl+1-9
    const digit = _input.match(/^[1-9]$/);
    if (key.ctrl && digit) {
      const n = parseInt(digit[0], 10) - 1; // 1 → index 0 (All)
      if (n < tabIds.length) {
        setActiveTab(tabIds[n] ?? 'all');
      }
    }
  });

  const handleSubmit = useCallback((text: string) => {
    const client = clientRef.current;
    if (!client) return;
    setIsLoading(true);
    // Reset agent-0 to running when a new request starts.
    setAgents((prev) => {
      const next = new Map(prev);
      const info = next.get('agent-0');
      if (info) {
        next.set('agent-0', { ...info, status: 'running' });
      }
      return next;
    });
    client.send(text);
  }, []);

  // Derive display events and label based on active tab.
  const allAgentEvents = activeTab === 'all' ? buildAllEvents(agents) : [];
  const activeAgentInfo = activeTab !== 'all' ? agents.get(activeTab) : undefined;
  const feedEvents = activeTab === 'all' ? allAgentEvents : (activeAgentInfo?.events ?? []);

  // Token usage display: per-agent when on specific tab, total on All.
  const displayTokens =
    activeTab === 'all'
      ? tokenUsage
      : (agentTokens.get(activeTab) ?? { input: 0, output: 0 });

  const activeAgentName =
    activeTab === 'all'
      ? 'all'
      : activeTab === 'agent-0'
      ? 'Team Lead'
      : (agents.get(activeTab)?.name ?? activeTab);

  const totalEventCount = activeTab === 'all'
    ? feedEvents.length
    : (activeAgentInfo?.events.length ?? 0);

  // Only show the tab bar if there are multiple agents or at least one subagent.
  const showTabBar = agents.size > 1;

  return (
    <Box flexDirection="column" height="100%">
      <Text bold color="cyan">fulcrumaxe TUI</Text>
      {showTabBar && (
        <TabBar agents={agents} activeTab={activeTab} onTabChange={setActiveTab} />
      )}
      <AgentFeed
        events={feedEvents}
        isConnected={isConnected}
      />
      <StatusBar
        isConnected={isConnected}
        isLoading={isLoading}
        eventCount={totalEventCount}
        tokenUsage={displayTokens}
        agentCount={agents.size}
        activeAgentName={activeAgentName}
        reconnectAttempt={reconnectAttempt}
        maxRetries={5}
        fatalMessage={fatalMessage}
        budgetPct={coordination.budgetPct}
        budgetWarn={coordination.budgetWarn}
        queueCounts={coordination.queueCounts}
        loopAgo={coordination.loopAgo}
      />
      <ChatInput onSubmit={handleSubmit} isDisabled={isLoading || !isConnected} />
    </Box>
  );
}

render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
