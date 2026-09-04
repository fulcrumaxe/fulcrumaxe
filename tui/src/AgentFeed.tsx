import React, { useMemo } from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import {
  BackendEvent,
  ThinkingEvent,
  ContentEvent,
  ToolUseEvent,
  ToolResultEvent,
  ReadyEvent,
  DoneEvent,
  ErrorEvent,
  AgentFeedFileEvent,
} from './types.js';

export interface AgentFeedProps {
  events: BackendEvent[];
  isConnected: boolean;
  /** When set, prefix each content/thinking/tool block with [agentLabel] for the unified feed. */
  agentLabel?: string;
}

/**
 * A "display block" is a processed unit ready to render.
 * Multiple adjacent content events are merged into one block.
 */
type DisplayBlock =
  | { kind: 'ready'; event: ReadyEvent; agentLabel?: string }
  | { kind: 'thinking'; text: string; active: boolean; agentLabel?: string }
  | { kind: 'content'; text: string; agentLabel?: string }
  | { kind: 'tool_use'; event: ToolUseEvent; agentLabel?: string }
  | { kind: 'tool_result'; event: ToolResultEvent; agentLabel?: string }
  | { kind: 'done'; event: DoneEvent; agentLabel?: string }
  | { kind: 'error'; event: ErrorEvent; agentLabel?: string }
  | { kind: 'agent_feed'; event: AgentFeedFileEvent; agentLabel?: string };

function summarizeTool(event: ToolUseEvent): string {
  const MAX = 80;
  const input = event.input;
  let summary: string;
  if (event.tool === 'bash' && typeof input['command'] === 'string') {
    summary = input['command'] as string;
  } else {
    summary = JSON.stringify(input);
  }
  return summary.length > MAX ? summary.slice(0, MAX - 1) + '…' : summary;
}

function truncateResult(result: string): { text: string; truncated: number } {
  const MAX_LINES = 20;
  const SHOW_LINES = 18;
  const lines = result.split('\n');
  if (lines.length <= MAX_LINES) {
    return { text: result, truncated: 0 };
  }
  return {
    text: lines.slice(0, SHOW_LINES).join('\n'),
    truncated: lines.length - SHOW_LINES,
  };
}

function buildDisplayBlocks(events: BackendEvent[]): DisplayBlock[] {
  const blocks: DisplayBlock[] = [];

  for (let i = 0; i < events.length; i++) {
    const ev = events[i];

    if (ev.type === 'usage') continue; // StatusBar will handle these

    if (ev.type === 'content') {
      // Merge all consecutive content events into one block.
      let text = ev.content;
      while (i + 1 < events.length && events[i + 1].type === 'content') {
        i++;
        text += (events[i] as ContentEvent).content;
      }
      blocks.push({ kind: 'content', text });
      continue;
    }

    if (ev.type === 'thinking') {
      // Merge consecutive thinking events.
      let text = ev.content;
      while (i + 1 < events.length && events[i + 1].type === 'thinking') {
        i++;
        text += (events[i] as ThinkingEvent).content;
      }
      // Active if the next non-usage event is also a thinking event or does not exist yet.
      const nextMeaningful = events.slice(i + 1).find((e) => e.type !== 'usage');
      const active =
        !nextMeaningful ||
        nextMeaningful.type === 'thinking';
      blocks.push({ kind: 'thinking', text, active });
      continue;
    }

    if (ev.type === 'ready') {
      blocks.push({ kind: 'ready', event: ev });
      continue;
    }
    if (ev.type === 'tool_use') {
      blocks.push({ kind: 'tool_use', event: ev });
      continue;
    }
    if (ev.type === 'tool_result') {
      blocks.push({ kind: 'tool_result', event: ev });
      continue;
    }
    if (ev.type === 'done') {
      blocks.push({ kind: 'done', event: ev });
      continue;
    }
    if (ev.type === 'error') {
      blocks.push({ kind: 'error', event: ev });
      continue;
    }

    if (ev.type === 'agent_feed') {
      blocks.push({ kind: 'agent_feed', event: ev });
      continue;
    }
  }

  return blocks;
}

function AgentPrefix({ label }: { label?: string }) {
  if (!label) return null;
  return <Text color="magenta" bold>[{label}] </Text>;
}

function ReadyBlock({ event, agentLabel }: { event: ReadyEvent; agentLabel?: string }) {
  return (
    <Box>
      <AgentPrefix label={agentLabel} />
      <Text color="green">
        Connected to {event.model} v{event.version}
      </Text>
    </Box>
  );
}

function ThinkingBlock({ text, active, agentLabel }: { text: string; active: boolean; agentLabel?: string }) {
  return (
    <Box>
      {active && (
        <Box marginRight={1}>
          <Text color="yellow">
            <Spinner type="dots" />
          </Text>
        </Box>
      )}
      <AgentPrefix label={agentLabel} />
      <Text dimColor italic>
        {text}
      </Text>
    </Box>
  );
}

function ContentBlock({ text, agentLabel }: { text: string; agentLabel?: string }) {
  if (agentLabel) {
    return (
      <Box>
        <AgentPrefix label={agentLabel} />
        <Text>{text}</Text>
      </Box>
    );
  }
  return <Text>{text}</Text>;
}

function ToolUseBlock({ event, agentLabel }: { event: ToolUseEvent; agentLabel?: string }) {
  return (
    <Box>
      <AgentPrefix label={agentLabel} />
      <Text bold color="cyan">
        {'> '}
        {event.tool}: {summarizeTool(event)}
      </Text>
    </Box>
  );
}

function ToolResultBlock({ event, agentLabel }: { event: ToolResultEvent; agentLabel?: string }) {
  const { text, truncated } = truncateResult(event.result);
  return (
    <Box flexDirection="column" paddingLeft={agentLabel ? 0 : 2}>
      {agentLabel && <AgentPrefix label={agentLabel} />}
      <Box paddingLeft={agentLabel ? 0 : 0}>
        <Text dimColor color={event.is_error ? 'red' : undefined}>
          {text}
        </Text>
      </Box>
      {truncated > 0 && (
        <Text dimColor>[{truncated} more lines]</Text>
      )}
    </Box>
  );
}

function DoneBlock({ agentLabel }: { agentLabel?: string }) {
  if (agentLabel) {
    return <Text dimColor>[{agentLabel}] {'─'.repeat(30)}</Text>;
  }
  return <Text dimColor>{'─'.repeat(40)}</Text>;
}

function ErrorBlock({ event, agentLabel }: { event: ErrorEvent; agentLabel?: string }) {
  return (
    <Box>
      <AgentPrefix label={agentLabel} />
      <Text bold color="red">
        Error: {event.error}
      </Text>
    </Box>
  );
}

/** Renders a file-sourced agent feed event — yellow to distinguish from live cyan RPC events. */
function AgentFeedBlock({ event, agentLabel }: { event: AgentFeedFileEvent; agentLabel?: string }) {
  return (
    <Box>
      <AgentPrefix label={agentLabel} />
      <Text color="yellow" bold>
        [{event.role}]
      </Text>
      <Text color="yellow"> {event.event}: </Text>
      <Text color="yellow" dimColor>
        {event.detail}
      </Text>
    </Box>
  );
}

const MAX_VISIBLE_BLOCKS = 50;

export function AgentFeed({ events, isConnected, agentLabel }: AgentFeedProps) {
  const blocks = useMemo(() => {
    const all = buildDisplayBlocks(events);
    // Only render last N blocks to prevent flickering from re-rendering hundreds of elements
    return all.length > MAX_VISIBLE_BLOCKS ? all.slice(-MAX_VISIBLE_BLOCKS) : all;
  }, [events]);

  return (
    <Box flexDirection="column">
      {!isConnected && events.length === 0 && (
        <Text dimColor>Connecting to backend...</Text>
      )}
      {blocks.map((block, idx) => {
        const label = agentLabel;
        switch (block.kind) {
          case 'ready':
            return <ReadyBlock key={idx} event={block.event} agentLabel={label} />;
          case 'thinking':
            return <ThinkingBlock key={idx} text={block.text} active={block.active} agentLabel={label} />;
          case 'content':
            return <ContentBlock key={idx} text={block.text} agentLabel={label} />;
          case 'tool_use':
            return <ToolUseBlock key={idx} event={block.event} agentLabel={label} />;
          case 'tool_result':
            return <ToolResultBlock key={idx} event={block.event} agentLabel={label} />;
          case 'done':
            return <DoneBlock key={idx} agentLabel={label} />;
          case 'error':
            return <ErrorBlock key={idx} event={block.event} agentLabel={label} />;
          case 'agent_feed':
            return <AgentFeedBlock key={idx} event={block.event} agentLabel={label} />;
          default:
            return null;
        }
      })}
    </Box>
  );
}
