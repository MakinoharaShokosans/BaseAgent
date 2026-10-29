/** 与后端 DTO 对应的类型。后端是事实来源，这里只是投影。 */

export type PermissionLevel = 'read_only' | 'workspace_write' | 'full_permissions';

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'waiting_for_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'terminated';

export const TERMINAL_STATUSES: readonly TaskStatus[] = [
  'succeeded',
  'failed',
  'cancelled',
  'terminated',
];

export interface Workspace {
  workspace_id: string;
  name: string;
  root_path: string;
  description: string;
  created_at: number;
}

export interface Session {
  session_id: string;
  workspace_id: string;
  created_at: number;
  /** 以下字段后端补齐前可能缺失 */
  title?: string | null;
  pinned?: boolean;
  archived?: boolean;
  updated_at?: number;
}

/** 工作区级设置；null 表示沿用上层（全局缺省） */
export interface WorkspaceSettings {
  permission_level: PermissionLevel | null;
  model: string | null;
  rules_enabled: boolean | null;
}

export interface Turn {
  id: number;
  task_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: number;
}

export interface PlanItem {
  id: number;
  title: string;
  status: 'pending' | 'in_progress' | 'completed';
}

export interface ApprovalRequest {
  approval_id: string;
  call_ids: string[];
  required_level: PermissionLevel;
  current_level: PermissionLevel;
  action_type: string;
  command: string;
  reason: string;
  escalation_count: number;
  related_actions: string[];
}

export interface Task {
  task_id: string;
  session_id: string;
  workspace_id: string;
  task_goal: string;
  status: TaskStatus;
  permission_level: PermissionLevel;
  approval_request: ApprovalRequest | null;
  plan: PlanItem[];
  step_count: number;
  total_tokens: number;
  consecutive_errors: number;
  termination_reason: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  artifact_count: number;
}

export interface DecisionResult {
  task_id: string;
  status: TaskStatus;
  message: string;
}

export interface WorkspaceMemory {
  profile: string[];
  guidelines: string[];
  architecture: string[];
  pitfalls: string[];
}

export interface SessionMemory {
  summary: string;
  watermark: number;
  facts: { text: string; task_id: string; trust: string; source: string }[];
  failures: { action: string; failure_reason: string; conclusion: string; task_id: string }[];
  pending_facts: { text: string; task_id: string; trust: string; source: string }[];
  pending_failures: {
    action: string;
    failure_reason: string;
    conclusion: string;
    task_id: string;
  }[];
}

export interface CompactionResult {
  status: 'compacted' | 'nothing_to_compact';
  /** 本次并入摘要的轮次数 */
  turns: number;
  memory: SessionMemory;
}

export interface TreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  children?: TreeNode[];
}

export interface FileContent {
  path: string;
  content: string;
  hash: string;
  size: number;
}

export interface DirectoryListing {
  path: string;
  parent: string | null;
  entries: { name: string; path: string; has_children: boolean }[];
  truncated: boolean;
}

export interface Artifact {
  artifact_id: string;
  size: number;
}

/** 事件信封：客户端必须忽略未知字段，并对未知事件名降级。 */
export interface ServerEvent {
  event: string;
  task_id: string;
  seq: number;
  ts: number;
  v?: number;
  [key: string]: unknown;
}

export interface ModelEndpoint {
  tier: string;
  name: string;
  model: string;
  context_window: number;
}

export interface SessionSettings {
  model: string | null;
  context_token_limit: number | null;
  defaults: { context_token_limit: number };
  bounds: { context_token_limit: { min: number; max: number; step: number } };
}

/** 会话下一次请求里各段提示词的 Token 估算，与真实任务同一套装配（GET /sessions/{id}/context） */
export interface ContextSegment {
  id:
    | 'system'
    | 'tools'
    | 'skills'
    | 'mcp_index'
    | 'project_rules'
    | 'workspace'
    | 'workspace_memory'
    | 'session_memory'
    | 'task_data'
    | 'history';
  tokens: number;
  /** system：系统提示词是否被用户改写过 */
  customized?: boolean;
  /** tools：工具个数 */
  count?: number;
  /** history：对话条数 */
  turns?: number;
  /** project_rules：检测状态 */
  status?: string;
}

export interface ContextSnapshot {
  segments: ContextSegment[];
  total_tokens: number;
  context_window: number;
  skipped: string[];
}

export interface AppSettings {
  system_prompt: string | null;
  system_prompt_default: string;
  system_prompt_max_chars: number;
}

export interface McpServer {
  name: string;
  transport: 'stdio' | 'sse';
  status: 'disabled' | 'idle' | 'connected' | 'failed';
  /** 以下字段后端补齐前可能缺失 */
  enabled?: boolean;
  exposure?: 'pinned' | 'deferred';
  command_preview?: string;
  disabled_tools?: string[];
  tool_count: number;
  tools: string[];
  rejected: { tool: string; reason: string }[];
  last_error: string;
  /** 失败后距离下次重试的秒数 */
  retry_in_sec: number;
}

export interface SkillInfo {
  name: string;
  source: 'builtin' | 'global' | 'workspace';
  description: string;
  triggers: string[];
  required_tools: string[];
  high_privilege: string[];
  warnings: string[];
  /** 后端补齐前缺失，视为启用 */
  enabled?: boolean;
  workspace_id?: string;
  workspace_name?: string;
}

export interface SkillSource {
  source: 'builtin' | 'global' | 'workspace';
  enabled: boolean;
  count: number;
  trusted: boolean;
}

export interface ToolInfo {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export type PromptKind = 'template' | 'dynamic' | 'fixed';

export interface PromptInfo {
  name: string;
  kind: PromptKind;
  title: string;
  usage: string;
  size: number;
  customized: boolean;
  max_chars: number;
}

export interface PromptDetail {
  name: string;
  content: string;
  /** 随发行版的初始版本 */
  default: string;
  customized: boolean;
  max_chars: number;
}

export interface ContextSegmentText {
  id: string;
  text: string;
  tokens: number;
}

export interface McpSettings {
  enabled: boolean;
}
