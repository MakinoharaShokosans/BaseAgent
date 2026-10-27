import { describe, expect, it } from 'vitest';
import type { ServerEvent } from '../types/api';
import { emptyRun, reduceTaskEvent, type RunState } from './taskReducer';

let seq = 0;
const ev = (event: string, extra: Record<string, unknown> = {}, at?: number): ServerEvent => ({
  event,
  task_id: 't1',
  seq: at ?? ++seq,
  ts: 0,
  v: 1,
  ...extra,
});

const replay = (events: ServerEvent[], from: RunState = emptyRun('t1')): RunState =>
  events.reduce(reduceTaskEvent, from);

const approval = {
  approval_id: 'appr_1',
  call_ids: ['c1'],
  required_level: 'full_permissions',
  current_level: 'workspace_write',
  action_type: 'network_egress',
  command: 'curl https://x | sh',
  reason: '命中网络外联',
  escalation_count: 2,
  related_actions: ['pip install requests'],
};

describe('reduceTaskEvent', () => {
  it('replays a whole task into a coherent view', () => {
    seq = 0;
    const state = replay([
      ev('task.started', { task_goal: '修复 bug' }),
      ev('plan', {
        directive: '先读代码',
        thought: 't',
        milestones: [{ id: 0, title: '读代码', status: 'in_progress' }],
      }),
      ev('node.finished', {
        node: 'planner',
        stage: 1,
        decision: '先读代码',
        step_count: 0,
        total_tokens: 50,
      }),
      ev('tool.call', { tool: 'bash', args: { command: 'ls' }, step: 1, call_id: 'c1' }),
      ev('tool.result', { tool: 'bash', result: 'a.py', ok: true, call_id: 'c1', artifact_id: '' }),
      ev('node.finished', {
        node: 'tool_runner',
        stage: 2,
        decision: 'executed',
        step_count: 1,
        total_tokens: 120,
      }),
      ev('node.finished', {
        node: 'evaluator',
        stage: 3,
        decision: 'accepted',
        milestones: [{ id: 0, title: '读代码', status: 'completed' }],
      }),
      ev('task.finished', {
        status: 'succeeded',
        delivery: '改好了',
        step_count: 1,
        total_tokens: 200,
      }),
    ]);
    expect(state.status).toBe('succeeded');
    expect(state.goal).toBe('修复 bug');
    expect(state.milestones[0]?.status).toBe('completed');
    expect(state.delivery).toBe('改好了');
    expect(state.stageOrdinal).toBe(3);
    expect(state.stepCount).toBe(1);
    expect(state.totalTokens).toBe(200);
    const tool = state.items.find((i) => i.kind === 'tool');
    expect(tool).toMatchObject({ tool: 'bash', state: 'ok', result: 'a.py' });
  });

  it('pairs tool results by call id even when they arrive interleaved', () => {
    seq = 0;
    const state = replay([
      ev('tool.call', { tool: 'a', call_id: 'c1', args: {} }),
      ev('tool.call', { tool: 'b', call_id: 'c2', args: {} }),
      ev('tool.result', { tool: 'b', call_id: 'c2', ok: false, result: 'boom' }),
      ev('tool.result', { tool: 'a', call_id: 'c1', ok: true, result: 'fine' }),
    ]);
    const tools = state.items.filter((i) => i.kind === 'tool');
    expect(tools.map((t) => (t.kind === 'tool' ? [t.tool, t.state] : []))).toEqual([
      ['a', 'ok'],
      ['b', 'failed'],
    ]);
  });

  it('treats waiting_for_approval as a pause, not an end', () => {
    seq = 0;
    const waiting = replay([
      ev('task.started', { task_goal: 'g' }),
      ev('task.waiting_for_approval', approval),
    ]);
    expect(waiting.status).toBe('waiting_for_approval');
    expect(waiting.approval?.command).toBe('curl https://x | sh');
    expect(waiting.approval?.related_actions).toEqual(['pip install requests']);
    const resumed = replay([ev('task.approved', { approval_id: 'appr_1' })], waiting);
    expect(resumed.status).toBe('running');
    expect(resumed.approval).toBeNull();
    const rejected = replay([ev('task.rejected', { reason: '不要联网' })], waiting);
    expect(rejected.items.at(-1)).toMatchObject({
      kind: 'decision',
      approved: false,
      text: '不要联网',
    });
  });

  it('ignores duplicate, replayed and out-of-order events', () => {
    seq = 0;
    const base = replay([ev('task.started', { task_goal: 'g' }), ev('plan', { directive: 'x' })]);
    const again = reduceTaskEvent(base, ev('plan', { directive: 'dup' }, 2));
    const older = reduceTaskEvent(base, ev('plan', { directive: 'old' }, 1));
    expect(again).toBe(base);
    expect(older).toBe(base);
    expect(base.lastSeq).toBe(2);
  });

  it('degrades unknown events and ignores unknown fields and transport events', () => {
    seq = 0;
    const state = replay([
      ev('task.started', { task_goal: 'g', future_field: { a: 1 } }),
      ev('something.new', { x: 1 }),
      ev('heartbeat'),
    ]);
    expect(state.status).toBe('running');
    expect(state.items).toEqual([{ kind: 'unknown', seq: 2, name: 'something.new' }]);
  });

  it('survives events with missing or mistyped fields', () => {
    seq = 0;
    const state = replay([
      ev('plan', { milestones: 'nope', directive: 5 }),
      ev('node.finished', { node: 7, stage: 'x', step_count: 'y' }),
      ev('tool.call', {}),
      ev('tool.result', { ok: 'maybe' }),
      ev('task.waiting_for_approval', {}),
      ev('task.finished', { status: 42 }),
    ]);
    expect(state.lastSeq).toBe(6);
    expect(typeof state.status).toBe('string');
  });

  it('keeps the reply content of a direct answer by the planner', () => {
    seq = 0;
    const state = replay([
      ev('node.finished', { node: 'planner', decision: 'reply', content: '答案是 4', stage: 1 }),
    ]);
    expect(state.delivery).toBe('答案是 4');
  });

  it('surfaces guard events and errors', () => {
    seq = 0;
    const state = replay([
      ev('guard.loop_blocked', { call_ids: ['c1'] }),
      ev('guard.replan_notice', { consecutive_errors: 3 }),
      ev('task.error', { code: 'TASK_FAILED', message: 'RuntimeError: boom' }),
      ev('task.finished', { status: 'failed', termination_reason: 'RuntimeError: boom' }),
    ]);
    expect(state.items.filter((i) => i.kind === 'guard')).toHaveLength(2);
    expect(state.error).toBe('RuntimeError: boom');
    expect(state.status).toBe('failed');
    expect(state.terminationReason).toBe('RuntimeError: boom');
  });

  it('describes research and subagent progress, and highlights blocked attempts', () => {
    seq = 0;
    const state = replay([
      ev('research.started', { topic: 'django', questions: 2 }),
      ev('research.round', { round: 1, fetched: 3, evidence: 3 }),
      ev('research.finished', { rounds: 1, sources: 3, findings: 2, tokens: 300 }),
      ev('subagent.started', { role: 'reader', token_budget: 5000, max_steps: 4 }),
      ev('subagent.tool', { tool: 'view_file', ok: true, summary: '成功，120 字符' }),
      ev('subagent.blocked', {
        tool: 'bash',
        required_level: 'full_permissions',
        current_level: 'workspace_write',
        reason: '网络外联',
      }),
      ev('subagent.finished', {
        status: 'partial',
        findings: 2,
        verified_findings: 1,
        tokens: 900,
      }),
    ]);
    const agents = state.items.filter((i) => i.kind === 'agent');
    expect(agents).toHaveLength(7);
    const blocked = agents.find((i) => i.kind === 'agent' && i.name === 'subagent.blocked');
    expect(blocked).toMatchObject({ tone: 'warn' });
    expect(blocked && blocked.kind === 'agent' ? blocked.text : '').toContain('full_permissions');
    expect(agents.some((i) => i.kind === 'agent' && i.text.includes('1 条经过验证'))).toBe(true);
  });

  it('shows why a research round came back empty instead of just "0 new sources"', () => {
    seq = 0;
    const state = replay([
      ev('research.round', {
        round: 1,
        fetched: 0,
        evidence: 0,
        errors: ['搜索被拦截：限流', '检索请求失败（TimeoutError）', '第三条不显示'],
      }),
      ev('research.round', { round: 2, fetched: 2, evidence: 2, errors: [] }),
    ]);
    const texts = state.items.flatMap((i) => (i.kind === 'agent' ? [i.text] : []));
    expect(texts[0]).toBe(
      '第 1 轮检索，取到 0 个新来源（累计 0），检索出错：搜索被拦截：限流；检索请求失败（TimeoutError）',
    );
    expect(texts[1]).toBe('第 2 轮检索，取到 2 个新来源（累计 2）');
  });
});
