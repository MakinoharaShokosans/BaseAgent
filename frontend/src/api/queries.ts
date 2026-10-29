/** 查询钩子：同一份数据在侧栏、对话、检视器、设置里共用缓存。 */
import { useQueries, useQuery } from '@tanstack/react-query';
import type { Session, Task, TaskStatus } from '../types/api';
import { isUnsupported, api } from './endpoints';

export function useWorkspaces() {
  return useQuery({ queryKey: ['workspaces'], queryFn: api.workspaces.list });
}

export function useSessions(workspaceId: string | null) {
  return useQuery({
    queryKey: ['sessions', workspaceId],
    queryFn: () => api.sessions.list(workspaceId as string),
    enabled: !!workspaceId,
  });
}

export function useSessionTasks(sessionId: string | null, limit = 20) {
  return useQuery({
    queryKey: ['tasks', sessionId, limit],
    queryFn: () => api.sessions.tasks(sessionId as string, limit),
    enabled: !!sessionId,
  });
}

/** 后端接口是否存在：404 / 405 视为“尚未提供”，界面据此置灰而不是报错 */
export function supportOf(query: { isError: boolean; error: unknown }): boolean {
  return !(query.isError && isUnsupported(query.error));
}

export interface Activity {
  status: TaskStatus | null;
  goal: string;
  at: number;
}

const IDLE: Activity = { status: null, goal: '', at: 0 };

/** 一批会话各自最近一个任务的状态，轮询刷新；页面不可见时暂停 */
export function useSessionActivity(sessionIds: string[]): Record<string, Activity> {
  const results = useQueries({
    queries: sessionIds.map((id) => ({
      queryKey: ['activity', id],
      queryFn: async (): Promise<Activity> => {
        const [latest] = await api.sessions.tasks(id, 1);
        return latest
          ? {
              status: latest.status,
              goal: latest.task_goal,
              at: latest.finished_at ?? latest.created_at,
            }
          : IDLE;
      },
      refetchInterval: (query: { state: { data?: Activity } }) => {
        if (typeof document !== 'undefined' && document.hidden) return false;
        const s = query.state.data?.status;
        return s === 'running' || s === 'queued' || s === 'waiting_for_approval' ? 2500 : 12000;
      },
      staleTime: 2000,
    })),
  });
  const out: Record<string, Activity> = {};
  sessionIds.forEach((id, i) => {
    out[id] = results[i]?.data ?? IDLE;
  });
  return out;
}

export function sessionTitle(session: Session, activity?: Activity): string {
  const title = session.title?.trim();
  if (title) return title;
  const goal = activity?.goal.trim();
  if (goal) return goal.length > 40 ? `${goal.slice(0, 40)}…` : goal;
  // 后端尚未提供标题时也要能区分空会话，不能一路都是“新会话”
  return `会话 ${session.session_id.slice(-4)}`;
}

/** 后端是否已提供标题 / 置顶 / 归档字段（旧后端连键都没有） */
export function hasSessionMeta(session: Session): boolean {
  return Object.prototype.hasOwnProperty.call(session, 'title');
}

export function isLive(status: TaskStatus | null | undefined): boolean {
  return status === 'running' || status === 'queued' || status === 'waiting_for_approval';
}

export function latestTask(tasks: Task[] | undefined): Task | undefined {
  return tasks?.[0];
}
