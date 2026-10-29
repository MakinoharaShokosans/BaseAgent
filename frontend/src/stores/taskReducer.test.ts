import { describe, expect, it } from 'vitest';
import type { ServerEvent } from '../types/api';
import { parseSay, emptyRun, reduceTaskEvent, type RunState } from './taskReducer';

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
      ev('plan.updated', { plan: [{ id: 1, title: '读代码', status: 'in_progress' }] }),
      ev('tool.call', { tool: 'bash', args: { command: 'ls' }, step: 1, call_id: 'c1' }),
      ev('tool.result', { tool: 'bash', result: 'a.py', ok: true, call_id: 'c1', artifact_id: '' }),
      ev('turn.finished', { turn: 1, tool_count: 1, failed_count: 0, tokens: 120 }),
      ev('plan.updated', { plan: [{ id: 1, title: '读代码', status: 'completed' }] }),
      ev('task.finished', {
        status: 'succeeded',
        delivery: '改好了',
        step_count: 1,
        total_tokens: 200,
      }),
    ]);
    expect(state.status).toBe('succeeded');
    expect(state.goal).toBe('修复 bug');
    expect(state.plan[0]?.status).toBe('completed');
    expect(state.delivery).toBe('改好了');
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
    const base = replay([ev('task.started', { task_goal: 'g' }), ev('plan.updated', { plan: [] })]);
    const again = reduceTaskEvent(base, ev('plan.updated', { plan: [{ id: 1 }] }, 2));
    const older = reduceTaskEvent(base, ev('plan.updated', { plan: [{ id: 2 }] }, 1));
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
      ev('plan.updated', { plan: 'nope' }),
      ev('turn.finished', { turn: 'x', tokens: 'y' }),
      ev('tool.call', {}),
      ev('tool.result', { ok: 'maybe' }),
      ev('task.waiting_for_approval', {}),
      ev('task.finished', { status: 42 }),
    ]);
    expect(state.lastSeq).toBe(6);
    expect(typeof state.status).toBe('string');
  });

  it('keeps the plan and the counters from turn and plan events', () => {
    seq = 0;
    const state = replay([
      ev('plan.updated', {
        plan: [
          { id: 1, title: 'a', status: 'completed' },
          { id: 2, title: 'b', status: 'weird' },
        ],
      }),
      ev('turn.finished', { turn: 3, tool_count: 1, failed_count: 0, tokens: 900 }),
    ]);
    expect(state.plan.map((p) => p.status)).toEqual(['completed', 'pending']);
    expect([state.stepCount, state.totalTokens]).toEqual([3, 900]);
    expect(state.items).toEqual([]); // 记账事件不进时间线
  });

  it('surfaces guard events and errors', () => {
    seq = 0;
    const state = replay([
      ev('guard.loop_blocked', { call_ids: ['c1'] }),
      ev('guard.error_notice', { consecutive_errors: 3 }),
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
      ev('websearch.started', { topic: 'django', questions: 2 }),
      ev('websearch.round', { round: 1, fetched: 3, evidence: 3 }),
      ev('websearch.finished', { rounds: 1, sources: 3, findings: 2, tokens: 300 }),
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
      ev('websearch.round', {
        round: 1,
        fetched: 0,
        evidence: 0,
        errors: ['搜索被拦截：限流', '检索请求失败（TimeoutError）', '第三条不显示'],
      }),
      ev('websearch.round', { round: 2, fetched: 2, evidence: 2, errors: [] }),
    ]);
    const texts = state.items.flatMap((i) => (i.kind === 'agent' ? [i.text] : []));
    expect(texts[0]).toBe(
      '第 1 轮检索，取到 0 个新来源（累计 0），检索出错：搜索被拦截：限流；检索请求失败（TimeoutError）',
    );
    expect(texts[1]).toBe('第 2 轮检索，取到 2 个新来源（累计 2）');
  });

  it("turns the model's remarks before tool calls into timeline narration, not the final reply", () => {
    seq = 0;
    const state = replay([
      ev('model.reply', {
        turn: 1,
        content: '现状：还没读过代码。下一步：先看入口文件。\n进度：了解结构 约 20%',
        tool_calls: [{ id: 'c1', name: 'list_dir' }],
      }),
      ev('model.reply', { turn: 2, content: '最终交付', tool_calls: [] }),
      ev('model.reply', { turn: 3, content: '', tool_calls: [{ id: 'c2', name: 'grep' }] }),
    ]);
    expect(state.items).toEqual([
      {
        kind: 'say',
        seq: 1,
        turn: 1,
        text: '现状：还没读过代码。下一步：先看入口文件。\n进度：了解结构 约 20%',
      },
    ]);
    expect(state.lastSeq).toBe(3); // 没有进时间线的回复照样推进序号
  });

  it('keeps internal bookkeeping events out of the timeline', () => {
    seq = 0;
    const state = replay([
      ev('llm.call', { purpose: 'main' }),
      ev('turn.finished', { turn: 1 }),
      ev('plan.updated', { plan: [] }),
    ]);
    expect(state.items).toEqual([]);
    expect(state.lastSeq).toBe(3);
  });

  it('shows a context compaction on the timeline and flags the degraded one', () => {
    seq = 0;
    const state = replay([
      ev('context.compacted', {
        turn: 9,
        steps: 6,
        cut: 12,
        before_tokens: 84000,
        summary_chars: 1800,
        via: 'llm',
      }),
      ev('context.compacted', {
        turn: 14,
        steps: 4,
        cut: 20,
        before_tokens: 90000,
        summary_chars: 700,
        via: 'mechanical',
      }),
    ]);
    const agents = state.items.flatMap((i) => (i.kind === 'agent' ? [i] : []));
    expect(agents[0]?.tone).toBe('info');
    expect(agents[0]?.text).toBe(
      '上下文压缩：较早的 6 步并成了一份摘要（压缩前约 84000 Token，摘要 1800 字符）',
    );
    expect(agents[1]?.tone).toBe('warn');
    expect(agents[1]?.text).toContain('摘要只是调用清单');
  });

  it('splits a narration into its text and the sub-task progress', () => {
    expect(parseSay('已完成：读了配置。下一步：改端口。\n进度：调整配置 约 60%')).toEqual({
      body: '已完成：读了配置。下一步：改端口。',
      progress: { label: '调整配置', percent: 60 },
    });
    expect(parseSay('> 已完成：A。\n> 进度：B 约 100%').progress).toEqual({
      label: 'B',
      percent: 100,
    });
    expect(parseSay('进度：超出 约 250%').progress?.percent).toBe(100);
    expect(parseSay('只有一句话，没有进度')).toEqual({
      body: '只有一句话，没有进度',
      progress: null,
    });
  });

  it('describes a web search by what was asked: a topic, urls, or both', () => {
    seq = 0;
    const state = replay([
      ev('websearch.started', { topic: 'django 迁移', questions: 2, urls: 0 }),
      ev('websearch.started', { topic: '读取指定网页：docs.example', questions: 0, urls: 2 }),
    ]);
    const texts = state.items.flatMap((i) => (i.kind === 'agent' ? [i.text] : []));
    expect(texts).toEqual([
      '开始联网搜索：django 迁移（2 个问题）',
      '开始联网搜索：读取指定网页：docs.example，读取 2 个网址',
    ]);
  });
});
