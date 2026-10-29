import { motion } from 'motion/react';
import { MessageSquareText } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { parseSay, type TimelineItem } from '../../stores/taskReducer';
import { cn, stripControl } from '../../utils/text';
import {
  AgentGlyph,
  CheckDraw,
  CrossGlyph,
  GaugeGlyph,
  GlobeGlyph,
  GuardGlyph,
  OrbitGlyph,
  ShieldCheckGlyph,
  TerminalGlyph,
  ToolGlyph,
} from '../icons/Glyphs';
import { Chevron, EASE_OUT, Reveal } from '../ui/primitives';

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
          'relative z-[1] mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-ink/[0.06] backdrop-blur shadow-sm dark:border-white/[0.08]',
          TONE_CLASS[tone],
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1 pt-1">{children}</div>
    </div>
  );
}

/** 工具调用的一行摘要：优先显示最能说明“在做什么”的那个参数，而不是整段 JSON */
function summarizeArgs(args: Record<string, unknown>): string {
  const first = (key: string) => (typeof args[key] === 'string' ? (args[key] as string) : null);
  const urls = Array.isArray(args['urls'])
    ? (args['urls'] as unknown[]).filter((u) => typeof u === 'string')
    : [];
  const pattern = first('pattern');
  const path = first('path');
  if (pattern && path) return `${pattern}  ·  ${path}`;
  return (
    pattern ??
    first('query') ??
    (urls.length ? `${urls[0]}${urls.length > 1 ? ` 等 ${urls.length} 个网址` : ''}` : null) ??
    path ??
    JSON.stringify(args)
  );
}

function ToolRow({ item }: { item: Extract<TimelineItem, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const command = typeof item.args['command'] === 'string' ? item.args['command'] : null;
  const summary = command ?? summarizeArgs(item.args);
  const Icon =
    item.tool === 'bash'
      ? TerminalGlyph
      : item.tool === 'websearch'
        ? GlobeGlyph
        : item.tool === 'spawn_subagent'
          ? AgentGlyph
          : ToolGlyph;
  const tone: RowTone = item.state === 'ok' ? 'ok' : item.state === 'failed' ? 'bad' : 'accent';
  return (
    <Row icon={<Icon size={14} />} tone={tone}>
      <button
        className="-mx-2 -my-1 flex w-[calc(100%+1rem)] items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors hover:bg-ink/[0.05]"
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
            <pre className="whitespace-pre-wrap break-all rounded-[14px] bg-code p-3.5 font-mono text-xs leading-relaxed text-code-ink border border-white/[0.06]">
              <span className="select-none text-accent font-semibold">$ </span>
              {stripControl(command)}
            </pre>
          )}
          {item.result !== undefined && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-[14px] bg-ink/[0.035] p-3.5 font-mono text-xs leading-relaxed border border-ink/[0.05]">
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

/** 模型每一步的汇报：正文加一条细进度。进度是模型对当前子任务的估计，不是精确值。 */
function SayRow({ text }: { text: string }) {
  const { body, progress } = parseSay(text);
  return (
    <Row icon={<MessageSquareText size={14} strokeWidth={1.7} />} tone="accent">
      <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-ink">
        {stripControl(body)}
      </p>
      {progress && (
        <div className="mt-1.5 flex items-center gap-2.5">
          <div
            role="progressbar"
            aria-label={`子任务进度${progress.label ? `：${progress.label}` : ''}`}
            aria-valuenow={progress.percent}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-[3px] w-24 overflow-hidden rounded-full bg-ink/[0.08]"
          >
            <motion.div
              className="h-full rounded-full bg-accent/70"
              initial={{ width: 0 }}
              animate={{ width: `${progress.percent}%` }}
              transition={{ duration: 0.5, ease: EASE_OUT }}
            />
          </div>
          <span className="text-2xs tabular-nums text-mute">
            {progress.label && <span className="mr-1.5">{stripControl(progress.label)}</span>}
            {progress.percent}%
          </span>
        </div>
      )}
    </Row>
  );
}

export function TraceTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <ol
      aria-label="执行过程"
      className="relative space-y-3 before:absolute before:bottom-3 before:left-3.5 before:top-3 before:w-px before:bg-gradient-to-b before:from-line before:via-line before:to-transparent"
    >
      {items.map((item) => (
        <motion.li
          key={`${item.kind}-${item.seq}`}
          className="relative"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.24, ease: EASE_OUT }}
        >
          {item.kind === 'say' && <SayRow text={item.text} />}
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
                item.name.startsWith('websearch') ? (
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
