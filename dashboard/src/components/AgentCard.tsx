import type { Agent } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Props {
  agent: Agent
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}m ${s}s`
}

export function AgentCard({ agent }: Props) {
  const statusMap: Record<Agent['status'], 'success' | 'info' | 'error' | 'neutral'> = {
    running: 'info',
    idle: 'neutral',
    error: 'error',
    done: 'success',
  }

  return (
    <div className="agent-card">
      <div className="agent-card-header">
        <span className="agent-card-role">{agent.role}</span>
        <StatusBadge status={statusMap[agent.status]} label={agent.status} />
      </div>
      <div className="agent-card-meta">
        <span className="agent-card-duration">{formatDuration(agent.duration)}</span>
        {agent.discussion && (
          <span className="agent-card-ref">Discussion #{agent.discussion}</span>
        )}
        {agent.pr && <span className="agent-card-ref">PR #{agent.pr}</span>}
      </div>
    </div>
  )
}
