import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';

export interface AgentInfo {
  id: string;
  name: string;
  parentId: string | null;
  status: 'running' | 'done' | 'error';
  events: import('./types.js').BackendEvent[];
}

export interface TabBarProps {
  agents: Map<string, AgentInfo>;
  activeTab: string;
  onTabChange: (tab: string) => void;
}

function StatusIndicator({ status }: { status: AgentInfo['status'] }) {
  if (status === 'running') {
    return (
      <Text color="yellow">
        <Spinner type="dots" />
      </Text>
    );
  }
  if (status === 'done') {
    return <Text color="green">✓</Text>;
  }
  // error
  return <Text color="red">✗</Text>;
}

export function TabBar({ agents, activeTab, onTabChange: _onTabChange }: TabBarProps) {
  const tabs: Array<{ id: string; label: string; status?: AgentInfo['status'] }> = [
    { id: 'all', label: 'All' },
  ];

  for (const [id, info] of agents) {
    const label = id === 'agent-0' ? 'Team Lead' : info.name;
    tabs.push({ id, label, status: info.status });
  }

  return (
    <Box flexDirection="row" borderStyle="single" borderBottom={true} borderTop={false} borderLeft={false} borderRight={false}>
      {tabs.map((tab, idx) => {
        const isActive = tab.id === activeTab;
        return (
          <Box key={tab.id} marginRight={1}>
            {idx > 0 && <Text dimColor> | </Text>}
            <Text bold={isActive} underline={isActive} color={isActive ? 'cyan' : undefined}>
              [{tab.label}
              {tab.status && (
                <>
                  {' '}
                  <StatusIndicator status={tab.status} />
                </>
              )}
              ]
            </Text>
          </Box>
        );
      })}
      <Text dimColor> (Ctrl+←/→ to switch)</Text>
    </Box>
  );
}
