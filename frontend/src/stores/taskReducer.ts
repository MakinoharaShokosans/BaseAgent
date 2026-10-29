/**
 * 事件到界面状态的归约：纯函数，与存储解耦，可以直接用事件序列回放测试。
 * 契约见 docs/09_api/02_events_contract.md。
 *
 * 规则：忽略未知事件和未知字段；按 seq 去重（重复或回退的事件不改变状态）；
 * 回合数与 Token 取自 `turn.finished` 与 `task.finished`，不用事件条数估计。
 */
import type { ApprovalRequest, PlanItem, ServerEvent, TaskStatus } from '../types/api';

export interface ChangedFile {
  path: string;
  added?: number;
  removed?: number;
}

export type AgentGroup = 'websearch' | 'subagent' | 'compaction';

export type TimelineItem =
  /** 思考链：流式时逐段追加，model.reply 到达后以权威内容覆盖 */
  | {
      kind: 'reasoning';
      seq: number;
      turn: number;
      text: string;
      streaming: boolean;
      startTs: number;
      endTs?: number;
    }
  /** 模型每一步的汇报（带工具调用的回复里的话） */
  | { kind: 'say'; seq: number; turn: number; text: string; streaming: boolean }
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
      startTs: number;
      endTs?: number;
      durationMs?: number;
      exitCode?: number;
      timedOut?: boolean;
      truncated?: boolean;
      bytes?: number;
      changedFiles?: ChangedFile[];
      folded?: boolean;
      /** 命令执行期间的实时输出（瞬态，刷新后只剩 result） */
      stdout: string;
      stderr: string;
    }
  | { kind: 'decision'; seq: number; approved: boolean; text: string }
  | { kind: 'guard'; seq: number; name: string; text: string }
  | {
      kind: 'agent';
      seq: number;
      group: AgentGroup;
      name: string;
      text: string;
      tone: 'info' | 'warn';
    }
  | { kind: 'unknown'; seq: number; name: string };

export interface RunState {
  taskId: string;
  lastSeq: number;
  status: TaskStatus;
  goal: string;
  plan: PlanItem[];
  items: TimelineItem[];
  approval: ApprovalRequest | null;
  delivery: string;
  terminationReason: string;
  error: string | null;
  stepCount: number;
  totalTokens: number;
  /** 事件时间戳（秒） */
  startedTs: number | null;
  finishedTs: number | null;
  compactions: number;
}

export function emptyRun(taskId: string, goal = '', status: TaskStatus = 'running'): RunState {
  return {
    taskId,
    lastSeq: 0,
    status,
    goal,
    plan: [],
    items: [],
    approval: null,
    delivery: '',
    terminationReason: '',
    error: null,
    stepCount: 0,
    totalTokens: 0,
    startedTs: null,
    finishedTs: null,
    compactions: 0,
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

function planOf(value: unknown): PlanItem[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.map((raw, index) => {
    const m = record(raw);
    const status = str(m['status'], 'pending');
    return {
      id: num(m['id'], index),
      title: str(m['title']),
      status: (['pending', 'in_progress', 'completed'].includes(status)
        ? status
        : 'pending') as PlanItem['status'],
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

/** 流式增量：追加到该回合最后一个仍在流式的同类块，没有就新建 */
function applyDelta(state: RunState, event: ServerEvent): RunState {
  const turn = num(event['turn']);
  const kind = event['kind'] === 'reasoning' ? 'reasoning' : 'content';
  const text = str(event['text']);
  if (!text) return state;
  const items = [...state.items];
  const want = kind === 'reasoning' ? 'reasoning' : 'say';
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (
      it &&
      (it.kind === 'reasoning' || it.kind === 'say') &&
      it.turn === turn &&
      it.kind === want &&
      it.streaming
    ) {
      items[i] = { ...it, text: it.text + text };
      return { ...state, items };
    }
    if (it && it.kind === 'tool') break; // 工具调用之后的文字属于新的块
  }
  const base = { seq: 0, turn, text, streaming: true };
  items.push(
    want === 'reasoning'
      ? { kind: 'reasoning', ...base, startTs: event.ts }
      : { kind: 'say', ...base },
  );
  return { ...state, items };
}

function appendOutput(state: RunState, event: ServerEvent): RunState {
  const text = str(event['text']);
  if (!text) return state;
  const callId = str(event['call_id']);
  const field = event['stream'] === 'stderr' ? 'stderr' : 'stdout';
  const items = [...state.items];
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it?.kind === 'tool' && it.state === 'running' && (!callId || it.callId === callId)) {
      items[i] = { ...it, [field]: it[field] + text };
      return { ...state, items };
    }
  }
  return state;
}

function changedFilesOf(value: unknown): ChangedFile[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: ChangedFile[] = [];
  for (const raw of value) {
    if (typeof raw === 'string') out.push({ path: raw });
    else {
      const m = record(raw);
      if (typeof m['path'] === 'string')
        out.push({
          path: m['path'],
          added: typeof m['added'] === 'number' ? m['added'] : undefined,
          removed: typeof m['removed'] === 'number' ? m['removed'] : undefined,
        });
    }
  }
  return out;
}

export function reduceTaskEvent(state: RunState, event: ServerEvent): RunState {
  // 瞬态事件：不带序号、不持久化，不推进游标
  if (event.event === 'model.delta') return applyDelta(state, event);
  if (event.event === 'tool.output') return appendOutput(state, event);
  if (typeof event.seq !== 'number' || event.seq <= state.lastSeq) return state;
  const next: RunState = { ...state, lastSeq: event.seq };
  const seq = event.seq;
  switch (event.event) {
    case 'model.reply': {
      const turn = num(event['turn']);
      const text = str(event['content']).trim();
      const reasoning = str(event['reasoning']).trim();
      const calls = Array.isArray(event['tool_calls']) ? event['tool_calls'].length : 0;
      // 去掉本回合流式累计的块，以权威内容重建
      const prior = state.items.find((i) => i.kind === 'reasoning' && i.turn === turn);
      const items = state.items.filter(
        (i) => !((i.kind === 'reasoning' || i.kind === 'say') && i.turn === turn && i.streaming),
      );
      if (reasoning)
        items.push({
          kind: 'reasoning',
          seq,
          turn,
          text: reasoning,
          streaming: false,
          startTs: prior?.kind === 'reasoning' ? prior.startTs : event.ts,
          endTs: event.ts,
        });
      if (text && calls) items.push({ kind: 'say', seq, turn, text, streaming: false });
      // 没有工具调用的回复是最终交付；task.finished 会给出脱敏后的版本并覆盖
      return { ...next, items, delivery: text && !calls ? text : state.delivery };
    }
    case 'plan.updated':
      return { ...next, plan: planOf(event['plan']) ?? state.plan };
    case 'turn.finished':
      return {
        ...next,
        stepCount: num(event['turn'], state.stepCount),
        totalTokens: num(event['tokens'], state.totalTokens),
      };
    // 与界面无关的内部事件：只推进序号，不进时间线
    case 'llm.call':
      return next;
    case 'task.started':
      return {
        ...next,
        status: 'running',
        goal: str(event['task_goal'], state.goal),
        error: null,
        startedTs: event.ts,
      };
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
            startTs: event.ts,
            stdout: '',
            stderr: '',
          },
        ],
      };
    case 'tool.result': {
      const callId = str(event['call_id']);
      const ok = event['ok'] !== false;
      const detail = {
        state: ok ? ('ok' as const) : ('failed' as const),
        result: str(event['result']),
        artifactId: str(event['artifact_id']) || undefined,
        endTs: event.ts,
        durationMs: typeof event['duration_ms'] === 'number' ? event['duration_ms'] : undefined,
        exitCode: typeof event['exit_code'] === 'number' ? event['exit_code'] : undefined,
        timedOut: event['timed_out'] === true || undefined,
        truncated: event['truncated'] === true || undefined,
        bytes: typeof event['bytes'] === 'number' ? event['bytes'] : undefined,
        changedFiles: changedFilesOf(event['changed_files']),
        folded: event['folded'] === true || undefined,
      };
      let matched = false;
      const items = state.items.map((item) => {
        if (item.kind === 'tool' && item.callId === callId && item.state === 'running') {
          matched = true;
          return { ...item, ...detail };
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
          startTs: event.ts,
          stdout: '',
          stderr: '',
          ...detail,
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
        finishedTs: event.ts,
        // 流式块若因中断没被 model.reply 收尾，到终态一律收尾
        items: state.items.map((i) =>
          (i.kind === 'reasoning' || i.kind === 'say') && i.streaming
            ? { ...i, streaming: false }
            : i,
        ),
      };
    }
    case 'task.error':
      return { ...next, error: str(event['message'], '任务出错') };
    case 'heartbeat':
    case 'stream.dropped':
      return state; // 传输事件不改变业务状态，也不推进序号
    default:
      if (
        event.event.startsWith('websearch.') ||
        event.event.startsWith('subagent.') ||
        event.event === 'context.compacted'
      ) {
        return {
          ...next,
          compactions: state.compactions + (event.event === 'context.compacted' ? 1 : 0),
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

function describeAgent(event: ServerEvent): {
  group: AgentGroup;
  name: string;
  text: string;
  tone: 'info' | 'warn';
} {
  const raw = describeAgentText(event);
  const group: AgentGroup = event.event.startsWith('websearch.')
    ? 'websearch'
    : event.event.startsWith('subagent.')
      ? 'subagent'
      : 'compaction';
  return { group, ...raw };
}

function describeAgentText(event: ServerEvent): {
  name: string;
  text: string;
  tone: 'info' | 'warn';
} {
  const name = event.event;
  switch (name) {
    case 'websearch.started':
      return {
        name,
        tone: 'info',
        text:
          `开始联网搜索：${str(event['topic'])}` +
          (num(event['urls']) ? `，读取 ${num(event['urls'])} 个网址` : '') +
          (num(event['questions']) ? `（${num(event['questions'])} 个问题）` : ''),
      };
    case 'websearch.round':
      return {
        name,
        tone: 'info',
        text:
          `第 ${num(event['round'])} 轮检索，取到 ${num(event['fetched'])} 个新来源（累计 ${num(event['evidence'])}）` +
          roundErrors(event),
      };
    case 'websearch.finished':
      return {
        name,
        tone: 'info',
        text: `研究结束：${num(event['rounds'])} 轮，${num(event['sources'])} 个来源，${num(event['findings'])} 条结论，用了 ${num(event['tokens'])} Token`,
      };
    case 'context.compacted': {
      // 机械清单没有各步要点，要让用户知道这次压缩是降级的
      const mechanical = event['via'] === 'mechanical';
      return {
        name,
        tone: mechanical ? 'warn' : 'info',
        text:
          `上下文压缩：较早的 ${num(event['steps'])} 步并成了一份摘要（压缩前约 ${num(event['before_tokens'])} Token，摘要 ${num(event['summary_chars'])} 字符）` +
          (mechanical ? '。模型总结不可用，摘要只是调用清单' : ''),
      };
    }
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
    case 'guard.error_notice':
      return `连续出错 ${num(event['consecutive_errors'])} 次，已提醒模型调整做法`;
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
  plan: PlanItem[];
  approval_request: ApprovalRequest | null;
  step_count: number;
  total_tokens: number;
  termination_reason: string;
  started_at?: number | null;
  finished_at?: number | null;
}): RunState {
  return {
    ...emptyRun(task.task_id, task.task_goal, task.status),
    plan: task.plan,
    approval: task.approval_request,
    stepCount: task.step_count,
    totalTokens: task.total_tokens,
    terminationReason: task.termination_reason,
    startedTs: task.started_at ?? null,
    finishedTs: task.finished_at ?? null,
  };
}

/** 本轮检索的失败原因（被拦截、服务不可用等），没有则为空串；让“取到 0 个”和“搜索坏了”看得出区别 */
function roundErrors(event: Record<string, unknown>): string {
  const errors = event['errors'];
  if (!Array.isArray(errors) || !errors.length) return '';
  return `，检索出错：${errors
    .filter((e) => typeof e === 'string')
    .slice(0, 2)
    .join('；')}`;
}

export interface SayParts {
  body: string;
  /** 当前子任务的名称与完成百分比（0–100）；模型没按格式写时为 null */
  progress: { label: string; percent: number } | null;
}

const PROGRESS_LINE = /^\s*>?\s*进度[:：]\s*(.*?)\s*约?\s*(\d{1,3})\s*%\s*$/m;

/** 把“……\n进度：子任务名 约 40%”拆成正文与进度；格式不对就原样当正文 */
export function parseSay(text: string): SayParts {
  const match = PROGRESS_LINE.exec(text);
  if (!match) return { body: text.trim(), progress: null };
  const percent = Math.min(100, Math.max(0, Number(match[2])));
  const body = text
    .replace(PROGRESS_LINE, '')
    .replace(/^\s*>\s?/gm, '')
    .trim();
  return { body, progress: { label: (match[1] ?? '').trim(), percent } };
}
