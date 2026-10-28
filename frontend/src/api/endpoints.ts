import { http, type HttpClient } from './http';
import type {
  AppSettings,
  Artifact,
  CompactionResult,
  ContextSnapshot,
  DecisionResult,
  DirectoryListing,
  FileContent,
  McpServer,
  ModelEndpoint,
  PermissionLevel,
  Session,
  SessionMemory,
  SessionSettings,
  Task,
  TreeNode,
  Turn,
  Workspace,
  WorkspaceMemory,
} from '../types/api';

const enc = encodeURIComponent;

export function createApi(client: HttpClient = http) {
  return {
    workspaces: {
      list: () => client.get<Workspace[]>('/workspaces'),
      create: (body: { name: string; root_path: string; description?: string }) =>
        client.post<Workspace>('/workspaces', body),
      update: (id: string, body: { name?: string; description?: string }) =>
        client.patch<Workspace>(`/workspaces/${enc(id)}`, body),
      remove: (id: string) => client.delete<void>(`/workspaces/${enc(id)}`),
      memory: (id: string) => client.get<WorkspaceMemory>(`/workspaces/${enc(id)}/memory`),
      addFact: (id: string, text: string, layer?: keyof WorkspaceMemory) =>
        client.post<WorkspaceMemory>(`/workspaces/${enc(id)}/memory/facts`, { text, layer }),
      removeMemory: (id: string, kind: keyof WorkspaceMemory, index: number) =>
        client.delete<WorkspaceMemory>(`/workspaces/${enc(id)}/memory/${kind}/${index}`),
      tree: (id: string) => client.get<TreeNode>(`/workspaces/${enc(id)}/files/tree`),
      readFile: (id: string, path: string) =>
        client.get<FileContent>(`/workspaces/${enc(id)}/files/content?path=${enc(path)}`),
      saveFile: (id: string, path: string, content: string, baseHash?: string) =>
        client.put<{ path: string; hash: string; size: number }>(
          `/workspaces/${enc(id)}/files/content`,
          { path, content, base_hash: baseHash },
        ),
    },
    sessions: {
      list: (workspaceId: string) =>
        client.get<Session[]>(`/workspaces/${enc(workspaceId)}/sessions`),
      create: (workspaceId: string) =>
        client.post<Session>(`/workspaces/${enc(workspaceId)}/sessions`),
      remove: (id: string) => client.delete<void>(`/sessions/${enc(id)}`),
      /** 复制会话为新会话；给出 fromTurnId 时只复制到该轮（含）为止 */
      fork: (id: string, fromTurnId?: number) =>
        client.post<Session>(
          `/sessions/${enc(id)}/fork`,
          fromTurnId === undefined ? undefined : { from_turn_id: fromTurnId },
        ),
      turns: (id: string, limit = 50, beforeId?: number) =>
        client.get<Turn[]>(
          `/sessions/${enc(id)}/turns?limit=${limit}${beforeId ? `&before_id=${beforeId}` : ''}`,
        ),
      memory: (id: string) => client.get<SessionMemory>(`/sessions/${enc(id)}/memory`),
      /** 手动压缩：除最近几轮外并入摘要；有任务在跑时 409，模型失败时 502 且记忆不变 */
      compact: (id: string) => client.post<CompactionResult>(`/sessions/${enc(id)}/memory/compact`),
      settings: (id: string) => client.get<SessionSettings>(`/sessions/${enc(id)}/settings`),
      saveSettings: (
        id: string,
        body: { model: string | null; context_token_limit: number | null },
      ) => client.put<SessionSettings>(`/sessions/${enc(id)}/settings`, body),
      /** 上下文检视：各段提示词的 Token 估算；后端检视器未启用时返回 503 */
      context: (id: string) => client.get<ContextSnapshot>(`/sessions/${enc(id)}/context`),
      tasks: (id: string, limit = 20) =>
        client.get<Task[]>(`/sessions/${enc(id)}/tasks?limit=${limit}`),
    },
    tasks: {
      submit: (
        sessionId: string,
        body: {
          task_goal: string;
          permission_level?: PermissionLevel;
          confirm_full_permissions?: boolean;
        },
      ) => client.post<Task>(`/sessions/${enc(sessionId)}/tasks`, body),
      get: (id: string) => client.get<Task>(`/tasks/${enc(id)}`),
      approve: (id: string, approvalId: string, decision: 'once' | 'always') =>
        client.post<DecisionResult>(`/tasks/${enc(id)}/approve`, {
          approval_id: approvalId,
          decision,
        }),
      reject: (id: string, approvalId: string, reason: string) =>
        client.post<DecisionResult>(`/tasks/${enc(id)}/reject`, {
          approval_id: approvalId,
          reason,
        }),
      resume: (id: string) => client.post<DecisionResult>(`/tasks/${enc(id)}/resume`),
      cancel: (id: string) => client.post<DecisionResult>(`/tasks/${enc(id)}/cancel`),
      remove: (id: string) => client.delete<void>(`/tasks/${enc(id)}`),
      artifacts: (id: string) => client.get<Artifact[]>(`/tasks/${enc(id)}/artifacts`),
      artifact: (id: string, artifactId: string) =>
        client.get<string>(`/tasks/${enc(id)}/artifacts/${enc(artifactId)}`),
    },
    system: {
      directories: (path?: string) =>
        client.get<DirectoryListing>(`/system/fs/directories${path ? `?path=${enc(path)}` : ''}`),
      /** 已装配的模型端点（不含密钥），取其中的 context_window */
      models: () => client.get<ModelEndpoint[]>('/models'),
      settings: () => client.get<AppSettings>('/settings'),
      saveSettings: (body: { system_prompt: string | null }) =>
        client.put<AppSettings>('/settings', body),
      mcpServers: () => client.get<McpServer[]>('/mcp/servers'),
      health: () => client.get<Record<string, string>>('/health/dependencies'),
      limits: () =>
        client.get<{
          /** 任务没有 Token、步数或时间预算（docs/12_decisions/0006） */
          max_concurrent_tasks: number;
        }>('/system/limits'),
    },
  };
}

export type Api = ReturnType<typeof createApi>;
export const api = createApi();
