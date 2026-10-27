/**
 * 事件到界面状态的归约：纯函数，与存储解耦，可以直接用事件序列回放测试。
 * 契约见 docs/09_api/02_events_contract.md。
 *
 * 规则：忽略未知事件和未知字段；按 seq 去重（重复或回退的事件不改变状态）；
 * 进度用 `node.finished` 的阶段序号，不用事件条数估计。
 */
import type { ApprovalRequest, Milestone, ServerEvent, TaskStatus } from '../types/api';

export type TimelineItem =
  | { kind: 'plan'; seq: number; directive: string; thought: string }
  | {
      kind: 'stage';
      seq: number;
      node: string;
      ordinal: number;
      decision: string;
      summary: string;
    }
  | {
      kind: 'tool';
      seq: number;
      callId: string;
      tool: string;
      args: Record<string, unknown>;
      step: number;
      state: 'running' | 'ok' | 'failed';
      result?: string;
      artifactId?: string;
    }
  | { kind: 'decision'; seq: number; approved: boolean; text: string }
  | { kind: 'guard'; seq: number; name: string; text: string }
  | { kind: 'agent'; seq: number; name: string; text: string; tone: 'info' | 'warn' }
  | { kind: 'unknown'; seq: number; name: string };

export interface RunState {
  taskId: string;
  lastSeq: number;
  status: TaskStatus;
  goal: string;
  milestones: Milestone[];
  items: TimelineItem[];
  approval: ApprovalRequest | null;
  delivery: string;
  terminationReason: string;
  error: string | null;
  stepCount: number;
  totalTokens: number;
  stageOrdinal: number;
}

export function emptyRun(taskId: string, goal = '', status: TaskStatus = 'running'): RunState {
  return {
    taskId,
    lastSeq: 0,
    status,
    goal,
    milestones: [],
    items: [],
    approval: null,
    delivery: '',
    terminationReason: '',
    error: null,
    stepCount: 0,
    totalTokens: 0,
    stageOrdinal: 0,
  };
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}
function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function milestonesOf(value: unknown): Milestone[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.map((raw, index) => {
    const m = record(raw);
    const status = str(m['status'], 'pending');
    return {
      id: num(m['id'], index),
      title: str(m['title']),
      description: str(m['description']),
      status: (['pending', 'in_progress', 'completed', 'failed'].includes(status)
        ? status
        : 'pending') as Milestone['status'],
    };
  });
}

function approvalOf(event: ServerEvent): ApprovalRequest {
  const list = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x)) : []);
  return {
    approval_id: str(event['approval_id']),
    call_ids: list(event['call_ids']),
    required_level: str(
      event['required_level'],
      'full_permissions',
    ) as ApprovalRequest['required_level'],
    current_level: str(
      event['current_level'],
      'workspace_write',
    ) as ApprovalRequest['current_level'],
    action_type: str(event['action_type'], 'unknown'),
    command: str(event['command']),
    reason: str(event['reason']),
    escalation_count: num(event['escalation_count'], 1),
    related_actions: list(event['related_actions']),
  };
}

export function reduceTaskEvent(state: RunState, event: ServerEvent): RunState {
  if (typeof event.seq !== 'number' || event.seq <= state.lastSeq) return state;
  const next: RunState = { ...state, lastSeq: event.seq };
  const seq = event.seq;
  switch (event.event) {
    case 'task.started':
      return { ...next, status: 'running', goal: str(event['task_goal'], state.goal), error: null };
    case 'plan': {
      const milestones = milestonesOf(event['milestones']) ?? state.milestones;
      return {
        ...next,
        milestones,
        items: [
          ...state.items,
          { kind: 'plan', seq, directive: str(event['directive']), thought: str(event['thought']) },
        ],
      };
    }
    case 'node.finished': {
      const milestones = milestonesOf(event['milestones']) ?? state.milestones;
      const ordinal = num(event['stage'], state.stageOrdinal + 1);
      const item: TimelineItem = {
        kind: 'stage',
        seq,
        node: str(event['node']),
        ordinal,
        decision: str(event['decision']),
        summary: str(event['output_summary']),
      };
      return {
        ...next,
        milestones,
        stageOrdinal: Math.max(state.stageOrdinal, ordinal),
        stepCount: num(event['step_count'], state.stepCount),
        totalTokens: num(event['total_tokens'], state.totalTokens),
        items: [...state.items, item],
        delivery:
          event['node'] === 'planner' && event['decision'] === 'reply'
            ? str(event['content'], state.delivery)
            : state.delivery,
      };
    }
    case 'tool.call':
      return {
        ...next,
        items: [
          ...state.items,
          {
            kind: 'tool',
            seq,
            callId: str(event['call_id'], `seq-${seq}`),
            tool: str(event['tool']),
            args: record(event['args']),
            step: num(event['step']),
            state: 'running',
          },
        ],
      };
    case 'tool.result': {
      const callId = str(event['call_id']);
      const ok = event['ok'] !== false;
      let matched = false;
      const items = state.items.map((item) => {
        if (item.kind === 'tool' && item.callId === callId && item.state === 'running') {
          matched = true;
          return {
            ...item,
            state: ok ? ('ok' as const) : ('failed' as const),
            result: str(event['result']),
            artifactId: str(event['artifact_id']) || undefined,
          };
        }
        return item;
      });
      if (!matched) {
        items.push({
          kind: 'tool',
          seq,
          callId: callId || `seq-${seq}`,
          tool: str(event['tool']),
          args: {},
          step: num(event['step']),
          state: ok ? 'ok' : 'failed',
          result: str(event['result']),
          artifactId: str(event['artifact_id']) || undefined,
        });
      }
      return { ...next, items };
    }
    case 'task.waiting_for_approval':
      // 挂起不是终态
      return { ...next, status: 'waiting_for_approval', approval: approvalOf(event) };
    case 'task.approved':
      return {
        ...next,
        status: 'running',
        approval: null,
        items: [...state.items, { kind: 'decision', seq, approved: true, text: '已批准' }],
      };
    case 'task.rejected':
      return {
        ...next,
        status: 'running',
        approval: null,
        items: [
          ...state.items,
          { kind: 'decision', seq, approved: false, text: str(event['reason'], '已拒绝') },
        ],
      };
    case 'task.finished': {
      const status = str(event['status'], 'succeeded') as TaskStatus;
      return {
        ...next,
        status,
        approval: null,
        delivery: str(event['delivery']) || state.delivery,
        terminationReason: str(event['termination_reason']),
        stepCount: num(event['step_count'], state.stepCount),
        totalTokens: num(event['total_tokens'], state.totalTokens),
      };
    }
    case 'task.error':
      return { ...next, error: str(event['message'], '任务出错') };
    case 'heartbeat':
    case 'stream.dropped':
      return state; // 传输事件不改变业务状态，也不推进序号
    default:
      if (event.event.startsWith('research.') || event.event.startsWith('subagent.')) {
        return {
          ...next,
          items: [...state.items, { kind: 'agent', seq, ...describeAgent(event) }],
        };
      }
      if (event.event.startsWith('guard.')) {
        return {
          ...next,
          items: [
            ...state.items,
            { kind: 'guard', seq, name: event.event, text: describeGuard(event) },
          ],
        };
      }
      // 未知事件：只显示名称，不报错
      return { ...next, items: [...state.items, { kind: 'unknown', seq, name: event.event }] };
  }
}

function describeAgent(event: ServerEvent): { name: string; text: string; tone: 'info' | 'warn' } {
  const name = event.event;
  switch (name) {
    case 'research.started':
      return {
        name,
        tone: 'info',
        text: `开始研究：${str(event['topic'])}（${num(event['questions'])} 个问题）`,
      };
    case 'research.round':
      return {
        name,
        tone: 'info',
        text: `第 ${num(event['round'])} 轮检索，取到 ${num(event['fetched'])} 个新来源（累计 ${num(event['evidence'])}）`,
      };
    case 'research.finished':
      return {
        name,
        tone: 'info',
        text: `研究结束：${num(event['rounds'])} 轮，${num(event['sources'])} 个来源，${num(event['findings'])} 条结论，用了 ${num(event['tokens'])} Token`,
      };
    case 'subagent.started':
      return {
        name,
        tone: 'info',
        text: `子智能体 ${str(event['role'])} 开始（预算 ${num(event['token_budget'])} Token，至多 ${num(event['max_steps'])} 步）`,
      };
    case 'subagent.step':
      return { name, tone: 'info', text: `子智能体第 ${num(event['step'])} 步` };
    case 'subagent.tool':
      return {
        name,
        tone: event['ok'] === false ? 'warn' : 'info',
        text: `子智能体调用 ${str(event['tool'])}：${str(event['summary'])}`,
      };
    case 'subagent.blocked':
      // 它解释了子任务为什么做不成某件事，要突出
      return {
        name,
        tone: 'warn',
        text: `子智能体被拒绝：${str(event['tool'])} 需要 ${str(event['required_level'])} 权限，当前只有 ${str(event['current_level'])}。${str(event['reason'])}`,
      };
    case 'subagent.finished':
      return {
        name,
        tone: 'info',
        text: `子智能体结束：${str(event['status'])}，${num(event['findings'])} 条结论（${num(event['verified_findings'])} 条经过验证），${num(event['tokens'])} Token`,
      };
    default:
      return { name, tone: 'info', text: name };
  }
}

function describeGuard(event: ServerEvent): string {
  switch (event.event) {
    case 'guard.loop_blocked':
      return '检测到重复的相同调用，已拦截';
    case 'guard.replan_notice':
      return `连续出错 ${num(event['consecutive_errors'])} 次，已通知重新规划`;
    case 'guard.approval_decided':
      return event['approved'] === true ? '审批已通过' : '审批被拒绝';
    default:
      return event.event;
  }
}

/** 由任务快照得到初始状态（刷新页面后恢复用）。 */
export function runFromSnapshot(task: {
  task_id: string;
  task_goal: string;
  status: TaskStatus;
  milestones: Milestone[];
  approval_request: ApprovalRequest | null;
  step_count: number;
  total_tokens: number;
  termination_reason: string;
}): RunState {
  return {
    ...emptyRun(task.task_id, task.task_goal, task.status),
    milestones: task.milestones,
    approval: task.approval_request,
    stepCount: task.step_count,
    totalTokens: task.total_tokens,
    terminationReason: task.termination_reason,
  };
}
