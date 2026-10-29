import { ApiError, http, type HttpClient } from './http';
import type {
  AppSettings,
  Artifact,
  CompactionResult,
  ContextSegmentText,
  ContextSnapshot,
  DecisionResult,
  DirectoryListing,
  FileContent,
  McpServer,
  McpSettings,
  ModelEndpoint,
  PermissionLevel,
  PromptDetail,
  PromptInfo,
  Session,
  SessionMemory,
  SessionSettings,
  SkillInfo,
  SkillSource,
  Task,
  ToolInfo,
  TreeNode,
  Turn,
  Workspace,
  WorkspaceMemory,
  WorkspaceSettings,
} from '../types/api';

const enc = encodeURIComponent;

/** 后端尚未提供的接口（404 / 405）：界面据此置灰对应控件，而不是报错。 */
export function isUnsupported(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405);
}

function createApi(client: HttpClient = http) {
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
      settings: (id: string) => client.get<WorkspaceSettings>(`/workspaces/${enc(id)}/settings`),
      saveSettings: (id: string, body: WorkspaceSettings) =>
        client.put<WorkspaceSettings>(`/workspaces/${enc(id)}/settings`, body),
    },
    sessions: {
      list: (workspaceId: string) =>
        client.get<Session[]>(`/workspaces/${enc(workspaceId)}/sessions`),
      get: (id: string) => client.get<Session>(`/sessions/${enc(id)}`),
      create: (workspaceId: string) =>
        client.post<Session>(`/workspaces/${enc(workspaceId)}/sessions`),
      update: (id: string, body: { title?: string; pinned?: boolean; archived?: boolean }) =>
        client.patch<Session>(`/sessions/${enc(id)}`, body),
      /** 按首轮对话生成标题（由对话所选模型生成） */
      generateTitle: (id: string) => client.post<Session>(`/sessions/${enc(id)}/title`),
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
      contextSegment: (id: string, segment: string) =>
        client.get<ContextSegmentText>(`/sessions/${enc(id)}/context/segments/${enc(segment)}`),
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
      traceUrl: (id: string) => client.url(`/tasks/${enc(id)}/trace`),
    },
    system: {
      directories: (path?: string) =>
        client.get<DirectoryListing>(`/system/fs/directories${path ? `?path=${enc(path)}` : ''}`),
      pickDirectory: () => client.post<{ path: string | null }>('/system/fs/pick-directory'),
      /** 已装配的模型端点（不含密钥） */
      models: () => client.get<ModelEndpoint[]>('/models'),
      settings: () => client.get<AppSettings>('/settings'),
      saveSettings: (body: { system_prompt: string | null }) =>
        client.put<AppSettings>('/settings', body),
      health: () => client.get<Record<string, string>>('/health/dependencies'),
      limits: () => client.get<{ max_concurrent_tasks: number }>('/system/limits'),
      tools: () => client.get<ToolInfo[]>('/tools'),
    },
    prompts: {
      list: () => client.get<PromptInfo[]>('/prompts'),
      get: (name: string) => client.get<PromptDetail>(`/prompts/${enc(name)}`),
      save: (name: string, content: string) =>
        client.put<PromptDetail>(`/prompts/${enc(name)}`, { content }),
      reset: (name: string) => client.delete<PromptDetail>(`/prompts/${enc(name)}`),
    },
    skills: {
      list: () => client.get<SkillInfo[]>('/skills'),
      sources: () => client.get<SkillSource[]>('/skills/sources'),
      setEnabled: (name: string, enabled: boolean) =>
        client.put<SkillInfo>(`/skills/${enc(name)}/enabled`, { enabled }),
      setSource: (source: string, enabled: boolean) =>
        client.put<SkillSource>(`/skills/sources/${enc(source)}`, { enabled }),
      body: (name: string, workspaceId?: string) =>
        client.get<{ name: string; content: string }>(
          `/skills/${enc(name)}${workspaceId ? `?workspace_id=${enc(workspaceId)}` : ''}`,
        ),
    },
    mcp: {
      servers: () => client.get<McpServer[]>('/mcp/servers'),
      settings: () => client.get<McpSettings>('/mcp/settings'),
      setEnabledAll: (enabled: boolean) => client.put<McpSettings>('/mcp/settings', { enabled }),
      update: (
        name: string,
        body: { enabled?: boolean; exposure?: 'pinned' | 'deferred'; disabled_tools?: string[] },
      ) => client.put<McpServer>(`/mcp/servers/${enc(name)}`, body),
      reconnect: (name: string) => client.post<McpServer>(`/mcp/servers/${enc(name)}/reconnect`),
    },
  };
}

export const api = createApi();
