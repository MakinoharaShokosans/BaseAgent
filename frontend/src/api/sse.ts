/**
 * SSE 管理器：订阅、重连（指数退避，上限 30 秒）、`Last-Event-ID`、序号连续性检测、
 * `STREAM_GAP` 与 `stream.dropped` 的处理，终态后停止。
 *
 * 不依赖 EventSource（它不能设置请求头），用 fetch 流式读取并自己解析帧。
 * 设计见 docs/09_api/02_events_contract.md §1、docs/10_frontend/01_architecture.md §8。
 */
import type { Fetch } from './http';
import type { ServerEvent } from '../types/api';

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'gap';

interface SseOptions {
  url: string;
  headers?: () => Record<string, string>;
  fetchImpl?: Fetch;
  /** 从哪个序号之后开始接收 */
  after?: number;
  onEvent: (event: ServerEvent) => void;
  onState?: (state: ConnectionState) => void;
  /** 游标已滑出缓冲：调用方应改用快照与时间线同步，之后重新订阅 */
  onGap?: () => void;
  sleep?: (ms: number) => Promise<void>;
  /** 测试用：覆盖退避计算 */
  backoff?: (attempt: number) => number;
}

export const MAX_BACKOFF_MS = 30_000;

export function defaultBackoff(attempt: number): number {
  return Math.min(MAX_BACKOFF_MS, 500 * 2 ** Math.min(attempt, 10));
}

export interface SseHandle {
  close(): void;
  readonly done: Promise<void>;
}

interface Frame {
  id?: string;
  event?: string;
  data: string;
}

/** 增量解析 SSE 文本流；跨块的帧会被拼接。 */
export class FrameParser {
  private buffer = '';

  push(chunk: string): Frame[] {
    this.buffer += chunk.replace(/\r\n/g, '\n');
    const frames: Frame[] = [];
    let cut = this.buffer.indexOf('\n\n');
    while (cut !== -1) {
      const block = this.buffer.slice(0, cut);
      this.buffer = this.buffer.slice(cut + 2);
      const frame = parseBlock(block);
      if (frame) frames.push(frame);
      cut = this.buffer.indexOf('\n\n');
    }
    return frames;
  }
}

function parseBlock(block: string): Frame | undefined {
  const frame: Frame = { data: '' };
  const data: string[] = [];
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'id') frame.id = value;
    else if (field === 'event') frame.event = value;
    else if (field === 'data') data.push(value);
  }
  if (!data.length && !frame.event) return undefined;
  frame.data = data.join('\n');
  return frame;
}

export function subscribe(options: SseOptions): SseHandle {
  const fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  const sleep = options.sleep ?? ((ms) => new Promise<void>((r) => setTimeout(r, ms)));
  const backoff = options.backoff ?? defaultBackoff;
  const controller = new AbortController();
  let cursor = options.after ?? 0;
  let finished = false;
  let gapped = false;

  const setState = (state: ConnectionState) => options.onState?.(state);

  async function run(): Promise<void> {
    let attempt = 0;
    while (!controller.signal.aborted && !finished) {
      setState(attempt === 0 ? 'connecting' : 'reconnecting');
      try {
        const response = await fetchImpl(options.url, {
          headers: {
            Accept: 'text/event-stream',
            ...(cursor > 0 ? { 'Last-Event-ID': String(cursor) } : {}),
            ...(options.headers?.() ?? {}),
          },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        setState('open');
        const outcome = await readStream(response.body);
        if (controller.signal.aborted) break;
        if (outcome === 'finished' || outcome === 'gap') {
          finished = true;
          gapped = outcome === 'gap';
          break;
        }
        if (outcome === 'progress' || outcome === 'reconnect') attempt = 0;
      } catch {
        if (controller.signal.aborted) break;
      }
      attempt += 1;
      setState('reconnecting');
      await sleep(backoff(attempt));
    }
    if (!controller.signal.aborted) setState(gapped ? 'gap' : finished ? 'closed' : 'reconnecting');
  }

  async function readStream(
    body: ReadableStream<Uint8Array>,
  ): Promise<'finished' | 'gap' | 'reconnect' | 'progress' | 'ended'> {
    const reader = body.getReader();
    const abort = () => void reader.cancel().catch(() => undefined);
    controller.signal.addEventListener('abort', abort, { once: true });
    const decoder = new TextDecoder();
    const parser = new FrameParser();
    let progressed = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return controller.signal.aborted ? 'ended' : progressed ? 'progress' : 'ended';
        for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
          const verdict = handleFrame(frame);
          if (verdict === 'progress') progressed = true;
          else if (verdict !== 'ignored') return verdict;
        }
      }
    } finally {
      controller.signal.removeEventListener('abort', abort);
      reader.cancel().catch(() => undefined);
    }
  }

  function handleFrame(frame: Frame): 'progress' | 'ignored' | 'finished' | 'gap' | 'reconnect' {
    if (frame.event === 'heartbeat') return 'ignored';
    let event: ServerEvent;
    try {
      event = JSON.parse(frame.data) as ServerEvent;
    } catch {
      return 'ignored'; // 损坏的帧：丢弃，不让整条流崩溃
    }
    const name = frame.event ?? event.event;
    if (name === 'stream.dropped') return 'reconnect'; // 慢消费者被丢了事件：带游标重连补齐
    if (name === 'task.error' && event['code'] === 'STREAM_GAP') {
      options.onGap?.();
      setState('gap');
      return 'gap';
    }
    // 瞬态事件（流式增量、命令输出）不带序号、不持久化：直接交付，不参与连续性检查
    if (name === 'model.delta' || name === 'tool.output') {
      options.onEvent({ ...event, event: name, seq: 0 });
      return 'ignored';
    }
    if (typeof event.seq !== 'number' || event.seq <= cursor) return 'ignored'; // 重复或乱序
    if (event.seq > cursor + 1 && cursor > 0) {
      return 'reconnect'; // 序号不连续：丢了事件，重连让服务端从事件库补齐
    }
    cursor = event.seq;
    options.onEvent(event);
    if (name === 'task.finished') {
      finished = true;
      return 'finished';
    }
    return 'progress';
  }

  const done = run();
  return {
    close() {
      controller.abort();
      setState('closed');
    },
    done,
  };
}
