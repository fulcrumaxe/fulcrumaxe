import type { WsEvent } from '../api/types'
import { formatTime } from '../lib/safeDate'

interface Props {
  event: WsEvent
}

export function EventLine({ event }: Props) {
  return (
    <div className={`event-line event-line--${event.event.split('.')[0]}`}>
      <span className="event-line-time">{formatTime(event.timestamp, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
      {event.role && (
        <span className={`event-line-role event-line-role--${event.role}`}>{event.role}</span>
      )}
      <span className="event-line-content">{event.content ?? event.event}</span>
    </div>
  )
}
