import { describe, expect, it } from 'vitest';
import type { ServerEvent } from '../types/api';
import {
  defaultBackoff,
  FrameParser,
  MAX_BACKOFF_MS,
  subscribe,
  type ConnectionState,
} from './sse';

const frame = (seq: number, event: string, extra: Record<string, unknown> = {}) =>
  `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify({ event, task_id: 't', seq, ts: 0, v: 1, ...extra })}\n\n`;

function streamOf(chunks: string[], hang = false): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      if (!hang) controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

interface Harness {
  events: ServerEvent[];
  states: ConnectionState[];
  requests: { url: string; headers: Record<string, string> }[];
  sleeps: number[];
  gaps: number;
}

function run(responses: (() => Response)[], options: { after?: number } = {}) {
  const h: Harness = { events: [], states: [], requests: [], sleeps: [], gaps: 0 };
  let call = 0;
  const handle = subscribe({
    url: '/api/v1/tasks/t/stream',
    after: options.after,
    headers: () => ({ Authorization: 'Bearer x' }),
    fetchImpl: async (url, init) => {
      h.requests.push({
        url: String(url),
        headers: { ...(init?.headers as Record<string, string>) },
      });
      const next = responses[Math.min(call++, responses.length - 1)];
      if (!next) throw new Error('no response scripted');
      return next();
    },
    onEvent: (e) => h.events.push(e),
    onState: (s) => h.states.push(s),
    onGap: () => (h.gaps += 1),
    sleep: async (ms) => {
      h.sleeps.push(ms);
    },
  });
  return { h, handle };
}

describe('FrameParser', () => {
  it('joins frames split across arbitrary chunk boundaries', () => {
    const text = frame(1, 'plan') + frame(2, 'tool.call');
    for (let cut = 1; cut < text.length; cut += 7) {
      const parser = new FrameParser();
      const frames = [...parser.push(text.slice(0, cut)), ...parser.push(text.slice(cut))];
      expect(frames.map((f) => f.id)).toEqual(['1', '2']);
    }
  });

  it('ignores comments, handles CRLF and multi-line data', () => {
    const parser = new FrameParser();
    const frames = parser.push(': ping\r\n\r\nevent: x\r\ndata: a\r\ndata: b\r\n\r\n');
    expect(frames).toEqual([{ event: 'x', data: 'a\nb' }]);
  });
});

describe('subscribe', () => {
  it('delivers events in order and stops after task.finished', async () => {
    const { h, handle } = run([
      () =>
        streamOf([
          frame(1, 'task.started'),
          frame(2, 'plan'),
          frame(3, 'task.finished', { status: 'succeeded' }),
        ]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(h.requests).toHaveLength(1);
    expect(h.states.at(-1)).toBe('closed');
  });

  it('reconnects with Last-Event-ID and the auth header after the stream drops', async () => {
    const { h, handle } = run([
      () => streamOf([frame(1, 'task.started'), frame(2, 'plan')]),
      () => streamOf([frame(3, 'tool.call'), frame(4, 'task.finished')]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(h.requests[0]?.headers['Last-Event-ID']).toBeUndefined();
    expect(h.requests[1]?.headers['Last-Event-ID']).toBe('2');
    expect(h.requests[1]?.headers['Authorization']).toBe('Bearer x');
    expect(h.requests.every((r) => !r.url.includes('token'))).toBe(true);
    expect(h.states).toContain('reconnecting');
  });

  it('reconnects instead of skipping when it notices a sequence gap', async () => {
    const { h, handle } = run([
      () => streamOf([frame(1, 'task.started'), frame(2, 'plan'), frame(5, 'tool.result')]),
      () =>
        streamOf([
          frame(3, 'tool.call'),
          frame(4, 'tool.result'),
          frame(5, 'tool.result'),
          frame(6, 'task.finished'),
        ]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(h.requests[1]?.headers['Last-Event-ID']).toBe('2');
  });

  it('drops duplicate and stale events', async () => {
    const { h, handle } = run([
      () =>
        streamOf([
          frame(1, 'a'),
          frame(1, 'a'),
          frame(2, 'b'),
          frame(1, 'old'),
          frame(3, 'task.finished'),
        ]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('reconnects after stream.dropped and does not count it as an event', async () => {
    const { h, handle } = run([
      () =>
        streamOf([
          frame(1, 'a'),
          'event: stream.dropped\ndata: {"event":"stream.dropped","task_id":"t","seq":0,"ts":0,"from_seq":2,"to_seq":9}\n\n',
        ]),
      () => streamOf([frame(2, 'b'), frame(3, 'task.finished')]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(h.requests[1]?.headers['Last-Event-ID']).toBe('1');
  });

  it('stops and asks for a snapshot on STREAM_GAP', async () => {
    const { h, handle } = run([
      () =>
        streamOf([
          `event: task.error\ndata: ${JSON.stringify({ event: 'task.error', task_id: 't', seq: 0, code: 'STREAM_GAP' })}\n\n`,
        ]),
    ]);
    await handle.done;
    expect(h.gaps).toBe(1);
    expect(h.requests).toHaveLength(1);
    expect(h.states.at(-1)).toBe('gap');
  });

  it('ignores heartbeats and corrupt frames', async () => {
    const { h, handle } = run([
      () =>
        streamOf([
          'event: heartbeat\ndata: {"task_id":"t"}\n\n',
          'event: plan\ndata: {not json\n\n',
          frame(1, 'task.finished'),
        ]),
    ]);
    await handle.done;
    expect(h.events.map((e) => e.event)).toEqual(['task.finished']);
  });

  it('backs off exponentially on failures, capped at 30 seconds', async () => {
    const failures = Array.from({ length: 12 }, () => () => new Response('x', { status: 503 }));
    const { h, handle } = run([...failures, () => streamOf([frame(1, 'task.finished')])]);
    await handle.done;
    expect(h.sleeps[0]).toBe(1000);
    expect(Math.max(...h.sleeps)).toBe(MAX_BACKOFF_MS);
    expect(h.sleeps).toEqual([...h.sleeps].sort((a, b) => a - b));
    expect(h.events).toHaveLength(1);
  });

  it('resumes from a given cursor and can be closed by the caller', async () => {
    const { h, handle } = run([() => streamOf([frame(6, 'plan')], true)], { after: 5 });
    await new Promise((r) => setTimeout(r, 20));
    expect(h.requests[0]?.headers['Last-Event-ID']).toBe('5');
    expect(h.events.map((e) => e.seq)).toEqual([6]);
    handle.close();
    await handle.done;
    expect(h.states.at(-1)).toBe('closed');
  });
});

describe('defaultBackoff', () => {
  it('doubles and caps', () => {
    expect(defaultBackoff(1)).toBe(1000);
    expect(defaultBackoff(2)).toBe(2000);
    expect(defaultBackoff(50)).toBe(MAX_BACKOFF_MS);
  });
});
