/**
 * feedWatcher.ts — tail .autonomous-team/agent-feed.jsonl every 2 seconds.
 *
 * Tracks the last byte offset so each poll only reads new data.
 * If the file shrinks (rotation / truncation), offset resets to 0.
 * Silently skips malformed JSON lines.
 * If the file does not exist, does nothing until it appears.
 */

import * as fs from 'fs';

export interface FeedEvent {
  ts: string;
  agent: string;
  role: string;
  event: string;
  detail: string;
  discussion?: number;
}

/**
 * Start watching a JSONL feed file.
 *
 * @param feedPath   Absolute path to the .jsonl file.
 * @param onEvents   Callback fired with each batch of new parsed events.
 * @returns          Cleanup function — call it to stop polling.
 */
export function startFeedWatcher(
  feedPath: string,
  onEvents: (events: FeedEvent[]) => void,
): () => void {
  // On first load, start at current EOF so we only show new events.
  let offset = 0;
  let initialised = false;

  const poll = () => {
    let stat: fs.Stats;
    try {
      stat = fs.statSync(feedPath);
    } catch {
      // File does not exist yet — wait.
      return;
    }

    if (!initialised) {
      // Skip existing history on startup.
      offset = stat.size;
      initialised = true;
      return;
    }

    if (stat.size < offset) {
      // File was truncated or rotated — reset.
      offset = 0;
    }

    if (stat.size === offset) {
      // Nothing new.
      return;
    }

    const toRead = stat.size - offset;
    const buf = Buffer.allocUnsafe(toRead);

    let fd: number;
    try {
      fd = fs.openSync(feedPath, 'r');
    } catch {
      return;
    }

    let bytesRead = 0;
    try {
      bytesRead = fs.readSync(fd, buf, 0, toRead, offset);
    } finally {
      fs.closeSync(fd);
    }

    offset += bytesRead;

    const chunk = buf.slice(0, bytesRead).toString('utf8');
    const lines = chunk.split('\n').filter((l) => l.trim().length > 0);

    const events: FeedEvent[] = [];
    for (const line of lines) {
      try {
        const ev = JSON.parse(line) as FeedEvent;
        if (ev.ts && ev.agent && ev.role && ev.event && ev.detail) {
          events.push(ev);
        }
      } catch {
        // Malformed line — skip silently.
      }
    }

    if (events.length > 0) {
      onEvents(events);
    }
  };

  const intervalId = setInterval(poll, 2000);

  // Return cleanup function.
  return () => {
    clearInterval(intervalId);
  };
}
