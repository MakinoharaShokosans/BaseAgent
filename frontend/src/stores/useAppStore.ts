/** 当前任务的运行视图与提示。当前位置（工作区 / 会话）由路由决定，这里不再保存。 */
import { create } from 'zustand';
import { api } from '../api/endpoints';
import { subscribe, type ConnectionState, type SseHandle } from '../api/sse';
import { http } from '../api/http';
import type { PermissionLevel, Task } from '../types/api';
import { TERMINAL_STATUSES } from '../types/api';
import { usePendingStore } from './usePendingStore';
import { emptyRun, reduceTaskEvent, runFromSnapshot, type RunState } from './taskReducer';

interface AppState {
  /** run 属于哪个会话；切换会话时清空 */
  runSession: string | null;
  run: RunState | null;
  connection: ConnectionState;
  /** 最近一次操作的错误（含 trace_id），界面提示后可清除 */
  notice: { message: string; traceId?: string; tone?: 'error' | 'info' } | null;
  /** 轻量提示：info 是成功反馈（如“已复制”），自动消失；error 需要手动关闭 */
  notify: (message: string, tone?: 'error' | 'info') => void;
  submit: (
    sessionId: string,
    goal: string,
    level: PermissionLevel,
    confirmFull: boolean,
  ) => Promise<Task | null>;
  attach: (sessionId: string, task: Task) => Promise<void>;
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

  async function guarded<T>(action: () => Promise<T>): Promise<T | null> {
    try {
      return await action();
    } catch (error) {
      const e = error as { message?: string; traceId?: string };
      set({ notice: { message: e.message ?? String(error), traceId: e.traceId || undefined } });
      return null;
    }
  }

  return {
    runSession: null,
    run: null,
    connection: 'closed',
    notice: null,
    submit: (sessionId, goal, level, confirmFull) =>
      guarded(async () => {
        const task = await api.tasks.submit(sessionId, {
          task_goal: goal,
          permission_level: level,
          confirm_full_permissions: confirmFull,
        });
        set({ runSession: sessionId, run: emptyRun(task.task_id, task.task_goal, task.status) });
        usePendingStore.getState().clear();
        follow(task.task_id, 0);
        return task;
      }),
    attach: async (sessionId, task) => {
      await guarded(async () => {
        set({ runSession: sessionId, run: runFromSnapshot(task) });
        // 从头订阅：事件库里有完整历史，所以刷新后时间线也能还原
        follow(task.task_id, 0);
      });
    },
    approve: async (decision) => {
      await guarded(async () => {
        const run = get().run;
        if (!run?.approval) return;
        await api.tasks.approve(run.taskId, run.approval.approval_id, decision);
      });
    },
    reject: async (reason) => {
      await guarded(async () => {
        const run = get().run;
        if (!run?.approval) return;
        await api.tasks.reject(run.taskId, run.approval.approval_id, reason);
      });
    },
    resume: async () => {
      await guarded(async () => {
        const run = get().run;
        if (!run) return;
        await api.tasks.resume(run.taskId);
        follow(run.taskId, run.lastSeq);
      });
    },
    cancel: async () => {
      await guarded(async () => {
        const run = get().run;
        if (!run) return;
        await api.tasks.cancel(run.taskId);
      });
    },
    clearNotice: () => set({ notice: null }),
    notify: (message, tone = 'info') => set({ notice: { message, tone } }),
    detach: () => {
      stop();
      set({ runSession: null, run: null, connection: 'closed' });
    },
  };
});
