/** 界面状态与当前任务的运行视图。快照走 TanStack Query，进度走 SSE 归约。 */
import { create } from 'zustand';
import { api } from '../api/endpoints';
import { subscribe, type ConnectionState, type SseHandle } from '../api/sse';
import { http } from '../api/http';
import type { PermissionLevel, Task } from '../types/api';
import { TERMINAL_STATUSES } from '../types/api';
import { emptyRun, reduceTaskEvent, runFromSnapshot, type RunState } from './taskReducer';

interface AppState {
  workspaceId: string | null;
  sessionId: string | null;
  run: RunState | null;
  connection: ConnectionState;
  /** 最近一次操作的错误（含 trace_id），界面提示后可清除 */
  notice: { message: string; traceId?: string } | null;
  selectWorkspace: (id: string | null) => void;
  selectSession: (id: string | null) => void;
  submit: (goal: string, level: PermissionLevel, confirmFull: boolean) => Promise<void>;
  attach: (task: Task) => Promise<void>;
  approve: (decision: 'once' | 'always') => Promise<void>;
  reject: (reason: string) => Promise<void>;
  resume: () => Promise<void>;
  cancel: () => Promise<void>;
  clearNotice: () => void;
  detach: () => void;
}

let handle: SseHandle | null = null;

function stop(): void {
  handle?.close();
  handle = null;
}

export const useAppStore = create<AppState>((set, get) => {
  function follow(taskId: string, after: number): void {
    stop();
    handle = subscribe({
      url: http.url(`/tasks/${encodeURIComponent(taskId)}/stream`),
      headers: () => http.headers(),
      after,
      onEvent: (event) =>
        set((s) => (s.run && s.run.taskId === taskId ? { run: reduceTaskEvent(s.run, event) } : s)),
      onState: (connection) => set({ connection }),
      onGap: () => {
        // 游标滑出缓冲：用快照重新同步，再从最新序号订阅
        void api.tasks.get(taskId).then((task) => {
          set({ run: runFromSnapshot(task) });
          if (!TERMINAL_STATUSES.includes(task.status)) follow(taskId, 0);
        });
      },
    });
  }

  async function guarded(action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (error) {
      const e = error as { message?: string; traceId?: string };
      set({ notice: { message: e.message ?? String(error), traceId: e.traceId || undefined } });
    }
  }

  return {
    workspaceId: null,
    sessionId: null,
    run: null,
    connection: 'closed',
    notice: null,
    selectWorkspace: (id) => {
      stop();
      set({ workspaceId: id, sessionId: null, run: null, connection: 'closed' });
    },
    selectSession: (id) => {
      stop();
      set({ sessionId: id, run: null, connection: 'closed' });
    },
    submit: (goal, level, confirmFull) =>
      guarded(async () => {
        const sessionId = get().sessionId;
        if (!sessionId) throw new Error('请先选择或创建会话');
        const task = await api.tasks.submit(sessionId, {
          task_goal: goal,
          permission_level: level,
          confirm_full_permissions: confirmFull,
        });
        set({ run: emptyRun(task.task_id, task.task_goal, task.status) });
        follow(task.task_id, 0);
      }),
    attach: (task) =>
      guarded(async () => {
        set({ run: runFromSnapshot(task) });
        // 从头订阅：事件库里有完整历史，所以刷新后时间线也能还原
        follow(task.task_id, 0);
      }),
    approve: (decision) =>
      guarded(async () => {
        const run = get().run;
        if (!run?.approval) return;
        await api.tasks.approve(run.taskId, run.approval.approval_id, decision);
      }),
    reject: (reason) =>
      guarded(async () => {
        const run = get().run;
        if (!run?.approval) return;
        await api.tasks.reject(run.taskId, run.approval.approval_id, reason);
      }),
    resume: () =>
      guarded(async () => {
        const run = get().run;
        if (!run) return;
        await api.tasks.resume(run.taskId);
        follow(run.taskId, run.lastSeq);
      }),
    cancel: () =>
      guarded(async () => {
        const run = get().run;
        if (!run) return;
        await api.tasks.cancel(run.taskId);
      }),
    clearNotice: () => set({ notice: null }),
    detach: () => {
      stop();
      set({ connection: 'closed' });
    },
  };
});
