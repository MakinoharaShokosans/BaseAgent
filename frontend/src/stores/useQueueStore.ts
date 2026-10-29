/**
 * 提交队列：任务运行期间写下的消息先排队，当前任务正常结束后依次提交；每条都可以撤回。
 * 只存在前端（按会话保存在 localStorage）；后端始终只看到顺序提交的单个任务。
 * 任务失败 / 取消 / 终止时队列暂停，由用户决定继续还是清空。
 */
import { create } from 'zustand';
import type { PermissionLevel } from '../types/api';

export interface QueuedMessage {
  id: string;
  text: string;
  level: PermissionLevel;
  at: number;
}

interface SessionQueue {
  items: QueuedMessage[];
  paused: boolean;
}

interface QueueState {
  bySession: Record<string, SessionQueue>;
  enqueue: (sessionId: string, text: string, level: PermissionLevel) => void;
  remove: (sessionId: string, id: string) => void;
  move: (sessionId: string, id: string, delta: -1 | 1) => void;
  /** 放回队首（提交失败时） */
  unshift: (sessionId: string, message: QueuedMessage) => void;
  /** 取出并移除队首 */
  shift: (sessionId: string) => QueuedMessage | undefined;
  setPaused: (sessionId: string, paused: boolean) => void;
  clear: (sessionId: string) => void;
}

const KEY = 'baseagent.queue.v1';
const EMPTY: SessionQueue = { items: [], paused: false };

function load(): Record<string, SessionQueue> {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Record<string, SessionQueue>) : {};
  } catch {
    return {};
  }
}

function save(bySession: Record<string, SessionQueue>): void {
  try {
    const live = Object.fromEntries(
      Object.entries(bySession).filter(([, q]) => q.items.length > 0),
    );
    window.localStorage.setItem(KEY, JSON.stringify(live));
  } catch {
    // 存储不可用：队列只在本次页面内有效
  }
}

let counter = 0;

export const useQueueStore = create<QueueState>((set, get) => {
  const update = (sessionId: string, fn: (q: SessionQueue) => SessionQueue) =>
    set((s) => ({
      bySession: { ...s.bySession, [sessionId]: fn(s.bySession[sessionId] ?? EMPTY) },
    }));
  return {
    bySession: load(),
    enqueue: (sessionId, text, level) =>
      update(sessionId, (q) => ({
        ...q,
        items: [
          ...q.items,
          { id: `q${Date.now().toString(36)}${counter++}`, text, level, at: Date.now() },
        ],
      })),
    remove: (sessionId, id) =>
      update(sessionId, (q) => ({ ...q, items: q.items.filter((m) => m.id !== id) })),
    move: (sessionId, id, delta) =>
      update(sessionId, (q) => {
        const index = q.items.findIndex((m) => m.id === id);
        const target = index + delta;
        if (index < 0 || target < 0 || target >= q.items.length) return q;
        const items = [...q.items];
        const [item] = items.splice(index, 1);
        items.splice(target, 0, item as QueuedMessage);
        return { ...q, items };
      }),
    unshift: (sessionId, message) =>
      update(sessionId, (q) => ({ ...q, items: [message, ...q.items] })),
    shift: (sessionId) => {
      const head = get().bySession[sessionId]?.items[0];
      if (head) update(sessionId, (q) => ({ ...q, items: q.items.slice(1) }));
      return head;
    },
    setPaused: (sessionId, paused) => update(sessionId, (q) => ({ ...q, paused })),
    clear: (sessionId) => update(sessionId, () => EMPTY),
  };
});

useQueueStore.subscribe((s) => save(s.bySession));

export function queueOf(state: QueueState, sessionId: string | null): SessionQueue {
  return (sessionId && state.bySession[sessionId]) || EMPTY;
}
