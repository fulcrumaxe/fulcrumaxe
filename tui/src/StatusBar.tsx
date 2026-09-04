import React from 'react';
import { Box, Text } from 'ink';

export interface StatusBarProps {
  isConnected: boolean;
  isLoading: boolean;
  eventCount: number;
  tokenUsage: { input: number; output: number };
  /** Number of active agents (including Team Lead). */
  agentCount?: number;
  /** Name of the currently viewed agent tab (e.g. "all", "Team Lead", "executor"). */
  activeAgentName?: string;
  /** Current reconnect attempt number (1-based), set when reconnecting. */
  reconnectAttempt?: number;
  /** Max retries before giving up. */
  maxRetries?: number;
  /** Set when the backend has permanently failed after all retries. */
  fatalMessage?: string;
  /** Budget percentage (0-100). null = unavailable. */
  budgetPct?: number | null;
  /** True when budget is in warning zone (>= 60%). */
  budgetWarn?: boolean;
  /** Active and ready Discussion counts. null = unavailable. */
  queueCounts?: { active: number; ready: number } | null;
  /** Human-readable last loop time, e.g. "2m ago (38s)". null = unavailable. */
  loopAgo?: string | null;
}

export function StatusBar({
  isConnected,
  isLoading,
  eventCount,
  tokenUsage,
  agentCount,
  activeAgentName,
  reconnectAttempt,
  maxRetries,
  fatalMessage,
  budgetPct,
  budgetWarn: _budgetWarn,
  queueCounts,
  loopAgo,
}: StatusBarProps) {
  // Derive budget color from percentage thresholds.
  const budgetColor = budgetPct == null
    ? 'white'
    : budgetPct >= 80
    ? 'red'
    : budgetPct >= 60
    ? 'yellow'
    : 'green';

  const budgetLabel = budgetPct == null ? 'Budget: --' : `Budget: ${budgetPct}%`;
  const queueLabel = queueCounts == null
    ? 'Queue: --'
    : `Queue: ${queueCounts.active} active / ${queueCounts.ready} ready`;
  const loopLabel = loopAgo == null ? 'Loop: --' : `Loop: ${loopAgo}`;
  let connectionDot: React.ReactElement;
  if (fatalMessage) {
    connectionDot = <Text color="red">●</Text>;
  } else if (reconnectAttempt !== undefined && reconnectAttempt > 0) {
    connectionDot = <Text color="yellow">●</Text>;
  } else {
    connectionDot = isConnected
      ? <Text color="green">●</Text>
      : <Text color="red">●</Text>;
  }

  let statusText: React.ReactElement;
  if (fatalMessage) {
    statusText = <Text color="red">Backend failed after {maxRetries ?? 5} retries</Text>;
  } else if (reconnectAttempt !== undefined && reconnectAttempt > 0) {
    statusText = <Text color="yellow">reconnecting (attempt {reconnectAttempt}/{maxRetries ?? 5})...</Text>;
  } else {
    statusText = isLoading
      ? <Text color="yellow">⟳ working...</Text>
      : <Text>idle</Text>;
  }

  const showTokens = tokenUsage.input > 0 || tokenUsage.output > 0;

  return (
    <Box>
      <Text dimColor>
        {/* We render pieces manually to keep dimColor on separators */}
      </Text>
      {connectionDot}
      <Text dimColor>{' | '}</Text>
      {statusText}
      <Text dimColor>{' | '}</Text>
      <Text dimColor>{eventCount} events</Text>
      {agentCount !== undefined && agentCount > 0 && (
        <>
          <Text dimColor>{' | '}</Text>
          <Text dimColor>{agentCount} {agentCount === 1 ? 'agent' : 'agents'}</Text>
        </>
      )}
      {activeAgentName && (
        <>
          <Text dimColor>{' | '}</Text>
          <Text dimColor>viewing: {activeAgentName}</Text>
        </>
      )}
      {showTokens && (
        <>
          <Text dimColor>{' | '}</Text>
          <Text dimColor>
            ↑{tokenUsage.input} ↓{tokenUsage.output} tokens
          </Text>
        </>
      )}
      <Text dimColor>{' | '}</Text>
      <Text color={budgetColor}>{budgetLabel}</Text>
      <Text dimColor>{' | '}</Text>
      <Text dimColor>{queueLabel}</Text>
      <Text dimColor>{' | '}</Text>
      <Text dimColor>{loopLabel}</Text>
    </Box>
  );
}
