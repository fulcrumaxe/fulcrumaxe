import { EventEmitter } from 'events';
import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';
import * as path from 'path';
import * as fs from 'fs';
import { fileURLToPath } from 'node:url';
import { BackendEvent, ReadyEvent } from './types.js';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SESSION_PATH = path.join(REPO_ROOT, '.autonomous-team', 'session.json');

interface SessionData {
  session_id: string;
  created_at: string;
  iteration_count: number;
}

/** Read session.json and return the session_id if valid, otherwise null. */
function _readSessionId(): string | null {
  try {
    const raw = fs.readFileSync(SESSION_PATH, 'utf8');
    const data = JSON.parse(raw) as SessionData;
    if (typeof data.session_id === 'string' && data.session_id.length > 0) {
      return data.session_id;
    }
  } catch {
    // Missing, corrupt, or unreadable — fall through.
  }
  return null;
}

/** Write a new session.json (first iteration bootstrap). */
function _writeSession(sessionId: string): void {
  try {
    const dir = path.dirname(SESSION_PATH);
    fs.mkdirSync(dir, { recursive: true });
    const data: SessionData = {
      session_id: sessionId,
      created_at: new Date().toISOString(),
      iteration_count: 1,
    };
    const tmp = SESSION_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(tmp, SESSION_PATH);
  } catch (err) {
    // Non-fatal — session persistence is best-effort.
    console.error(`[backend] warning: could not write session.json: ${err}`);
  }
}

const MAX_RETRIES = 5;
const INITIAL_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 60000;

export class BackendClient extends EventEmitter {
  private child: ChildProcess | null = null;
  private reqCounter = 0;
  private readonly debugLog: string[] = [];
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionalStop = false;

  start(): void {
    this.intentionalStop = false;
    const serverPath = path.join(REPO_ROOT, 'backend', 'server.py');

    this.child = spawn('python3', [serverPath], {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        AF_API_KEY: process.env['AF_API_KEY'],
        AF_PROVIDER: process.env['AF_PROVIDER'],
        AF_MODEL: process.env['AF_MODEL'],
        AF_BASE_URL: process.env['AF_BASE_URL'],
        GH_REPO: 'autonomous-agent-7/autonomous-forever',
        AF_REQUEST_TIMEOUT: '0',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const rl = readline.createInterface({ input: this.child.stdout! });
    rl.on('line', (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let event: BackendEvent;
      try {
        event = JSON.parse(trimmed) as BackendEvent;
      } catch (err) {
        this.emit('error', new Error(`Protocol parse error: ${trimmed.slice(0, 200)}`));
        return;
      }
      this.emit('event', event);
      if (event.type === 'ready') {
        if (this.reconnectAttempts > 0) {
          this.emit('reconnected');
          this.reconnectAttempts = 0;
        }
        this.emit('ready', event as ReadyEvent);
      }
      // Bootstrap: when the server returns a done event with a session_id and we
      // don't yet have a session.json, write it so trigger.py can reuse it.
      if (event.type === 'done') {
        const doneEvent = event as BackendEvent & { session_id?: string };
        if (typeof doneEvent.session_id === 'string' && doneEvent.session_id.length > 0) {
          const existing = _readSessionId();
          if (!existing) {
            _writeSession(doneEvent.session_id);
          }
        }
      }
    });

    this.child.stderr!.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8');
      this.debugLog.push(text);
      // Keep debug log bounded.
      if (this.debugLog.length > 500) this.debugLog.shift();
    });

    this.child.on('exit', (code) => {
      rl.close();
      this.child = null;
      this.emit('exit', code);
      if (this.intentionalStop) {
        return;
      }
      if (code !== 0 && code !== null) {
        if (this.reconnectAttempts < MAX_RETRIES) {
          const attempt = this.reconnectAttempts + 1;
          const backoff = Math.min(INITIAL_BACKOFF_MS * Math.pow(2, this.reconnectAttempts), MAX_BACKOFF_MS);
          this.reconnectAttempts = attempt;
          this.emit('reconnecting', { attempt, maxRetries: MAX_RETRIES });
          this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.start();
          }, backoff);
        } else {
          this.emit('fatal', new Error(`Backend failed after ${MAX_RETRIES} retries`));
        }
      }
    });

    this.child.on('error', (err) => {
      this.emit('error', err);
    });
  }

  /**
   * Send a prompt to the backend server. Reads session_id from session.json
   * (if present) and includes it in the request payload so the server resumes
   * the existing conversation history.
   */
  send(prompt: string, sessionId?: string): string {
    if (!this.child || !this.child.stdin) {
      throw new Error('BackendClient is not started');
    }
    const id = `req-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    // Prefer the explicitly-passed sessionId, then fall back to session.json.
    const resolvedSessionId = sessionId ?? _readSessionId();
    const line = JSON.stringify({ id, prompt, session_id: resolvedSessionId }) + '\n';
    this.child.stdin.write(line);
    return id;
  }

  stop(): void {
    this.intentionalStop = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.child) {
      this.child.kill('SIGTERM');
      this.child = null;
    }
  }
}
