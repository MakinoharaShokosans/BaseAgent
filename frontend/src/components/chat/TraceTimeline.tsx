import { motion } from 'motion/react';
import { useState, type ReactNode } from 'react';
import type { Milestone } from '../../types/api';
import type { TimelineItem } from '../../stores/taskReducer';
import { cn, stripControl } from '../../utils/text';
import {
  AgentGlyph,
  CheckDraw,
  CrossGlyph,
  GaugeGlyph,
  GlobeGlyph,
  GuardGlyph,
  OrbitGlyph,
  PlanGlyph,
  ShieldCheckGlyph,
  TerminalGlyph,
  ToolGlyph,
} from '../icons/Glyphs';
import { Chevron, EASE_OUT, Reveal } from '../ui/primitives';

const NODE_LABELS: Record<string, string> = {
  planner: '规划',
  budget_guard: '预算检查',
  executor: '执行',
  tool_runner: '工具',
  evaluator: '验收',
};

export function MilestoneList({ milestones }: { milestones: Milestone[] }) {
  if (!milestones.length) return null;
  return (
    <ol aria-label="里程碑" className="space-y-1">
      {milestones.map((m) => (
        <li key={m.id} className="text-sm">
          {m.title}
        </li>
      ))}
    </ol>
  );
}

type RowTone = 'mute' | 'accent' | 'violet' | 'ok' | 'bad' | 'warn';

const TONE_CLASS: Record<RowTone, string> = {
  mute: 'text-mute bg-ink/[0.05]',
  accent: 'text-accent bg-accent/10',
  violet: 'text-accent-2 bg-accent-2/10',
  ok: 'text-ok bg-ok/10',
  bad: 'text-bad bg-bad/10',
  warn: 'text-warn bg-warn/10',
};

function Row({
  icon,
  tone = 'mute',
  children,
}: {
  icon: ReactNode;
  tone?: RowTone;
  children: ReactNode;
}) {
  return (
    <div className="flex items-start gap-3">
      <span
        className={cn(
          'relative z-[1] mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-full backdrop-blur',
          TONE_CLASS[tone],
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1 pt-1">{children}</div>
    </div>
  );
}

function ToolRow({ item }: { item: Extract<TimelineItem, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const command = typeof item.args['command'] === 'string' ? item.args['command'] : null;
  const summary =
    command ??
    (typeof item.args['path'] === 'string' ? item.args['path'] : JSON.stringify(item.args));
  const Icon =
    item.tool === 'bash'
      ? TerminalGlyph
      : item.tool === 'delegate_research'
        ? GlobeGlyph
        : item.tool === 'spawn_subagent'
          ? AgentGlyph
          : ToolGlyph;
  const tone: RowTone = item.state === 'ok' ? 'ok' : item.state === 'failed' ? 'bad' : 'accent';
  return (
    <Row icon={<Icon size={14} />} tone={tone}>
      <button
        className="-mx-2 -my-1 flex w-[calc(100%+1rem)] items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-ink/[0.04]"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <code className="font-mono text-[12.5px] font-semibold">{item.tool}</code>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-mute">
          {stripControl(summary)}
        </span>
        <span
          className={cn(
            'flex items-center gap-1 text-xs font-semibold',
            tone === 'ok' ? 'text-ok' : tone === 'bad' ? 'text-bad' : 'text-accent',
          )}
        >
          {item.state === 'running' ? (
            <OrbitGlyph size={13} />
          ) : item.state === 'ok' ? (
            <CheckDraw size={13} />
          ) : (
            <CrossGlyph size={13} />
          )}
          {item.state === 'running' ? '运行中' : item.state === 'ok' ? '完成' : '失败'}
        </span>
        <Chevron open={open} />
      </button>
      <Reveal open={open}>
        <div className="space-y-2 pt-2">
          {command && (
            <pre className="whitespace-pre-wrap break-all rounded-xl bg-code p-3 font-mono text-xs leading-relaxed text-code-ink">
              <span className="select-none text-accent">$ </span>
              {stripControl(command)}
            </pre>
          )}
          {item.result !== undefined && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-ink/[0.04] p-3 font-mono text-xs leading-relaxed">
              {stripControl(item.result)}
            </pre>
          )}
          {item.artifactId && (
            <p className="text-xs text-mute">完整输出已保存为产物 {item.artifactId}</p>
          )}
        </div>
      </Reveal>
    </Row>
  );
}

export function TraceTimeline({ items }: { items: TimelineItem[] }) {
  const visible = items.filter(
    (i) => !(i.kind === 'stage' && i.node === 'budget_guard' && i.decision === 'pass'),
  );
  return (
    <ol
      aria-label="执行过程"
      className="relative space-y-3 before:absolute before:bottom-3 before:left-3.5 before:top-3 before:w-px before:bg-gradient-to-b before:from-line before:via-line before:to-transparent"
    >
      {visible.map((item) => (
        <motion.li
          key={`${item.kind}-${item.seq}`}
          className="relative"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.24, ease: EASE_OUT }}
        >
          {item.kind === 'plan' && (
            <Row icon={<PlanGlyph size={14} />} tone="violet">
              <p className="text-[13.5px] leading-snug">
                <span className="mr-2 font-semibold text-accent-2">规划</span>
                {stripControl(item.directive || item.thought)}
              </p>
            </Row>
          )}
          {item.kind === 'stage' && (
            <Row icon={<GaugeGlyph size={14} />}>
              <p className="flex flex-wrap items-center gap-1.5 text-xs text-mute">
                <span className="font-mono tabular-nums text-mute/70">#{item.ordinal}</span>
                <span className="font-medium text-ink/80">
                  {NODE_LABELS[item.node] ?? item.node}
                </span>
                {item.decision && (
                  <span className="rounded-full bg-ink/[0.05] px-2 py-0.5 font-mono text-2xs">
                    {item.decision.slice(0, 40)}
                  </span>
                )}
              </p>
            </Row>
          )}
          {item.kind === 'tool' && <ToolRow item={item} />}
          {item.kind === 'decision' && (
            <Row
              icon={item.approved ? <ShieldCheckGlyph size={14} /> : <GuardGlyph size={14} />}
              tone={item.approved ? 'ok' : 'bad'}
            >
              <p
                className={cn(
                  'text-[13.5px] font-semibold',
                  item.approved ? 'text-ok' : 'text-bad',
                )}
              >
                {item.approved ? '已批准' : `已拒绝：${stripControl(item.text)}`}
              </p>
            </Row>
          )}
          {item.kind === 'guard' && (
            <Row icon={<GuardGlyph size={14} />} tone="warn">
              <p className="text-[13.5px] text-warn">{item.text}</p>
            </Row>
          )}
          {item.kind === 'agent' && (
            <Row
              icon={
                item.name.startsWith('research') ? (
                  <GlobeGlyph size={14} />
                ) : (
                  <AgentGlyph size={14} />
                )
              }
              tone={item.tone === 'warn' ? 'bad' : 'violet'}
            >
              <p
                className={cn(
                  'text-[13.5px] leading-snug',
                  item.tone === 'warn' && 'font-semibold text-bad',
                )}
              >
                {stripControl(item.text)}
              </p>
            </Row>
          )}
          {item.kind === 'unknown' && (
            <Row icon={<GaugeGlyph size={14} />}>
              <p className="font-mono text-xs text-mute">{item.name}</p>
            </Row>
          )}
        </motion.li>
      ))}
    </ol>
  );
}
