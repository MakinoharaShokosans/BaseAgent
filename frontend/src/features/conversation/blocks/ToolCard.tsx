/**
 * 工具调用卡片：一行摘要，展开看完整参数、输出、耗时、退出码、改动。
 * 不同工具有各自的展开形态；未知工具回退到“参数 + 结果”。工具输出一律按文本渲染。
 */
import { useMutation } from '@tanstack/react-query';
import {
  AlertTriangle,
  BookOpen,
  Bot,
  Check,
  FilePen,
  FilePlus,
  FileText,
  FolderTree,
  Globe,
  Loader2,
  Plug,
  Search,
  Shield,
  SquareTerminal,
  Wrench,
  X,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { api } from '../../../api/endpoints';
import { CopyButton } from '../../../components/ui/CopyButton';
import { Tip } from '../../../components/ui/Tooltip';
import { Chevron, Reveal } from '../../../components/ui/primitives';
import type { TimelineItem } from '../../../stores/taskReducer';
import { cn, stripControl } from '../../../utils/text';
import { diffLines, diffStats } from '../../../utils/diff';
import { clock, formatBytes, formatDuration } from '../../../utils/time';

type Item = Extract<TimelineItem, { kind: 'tool' }>;

const ICONS: Record<string, typeof Wrench> = {
  bash: SquareTerminal,
  view_file: FileText,
  write_file: FilePlus,
  edit_file: FilePen,
  list_dir: FolderTree,
  grep: Search,
  load_skill: BookOpen,
  websearch: Globe,
  delegate: Bot,
  read_artifact: FileText,
};

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v);
}

/** 一行摘要：最能说明“这次调用做了什么”的参数 */
function summaryOf(item: Item): string {
  const a = item.args;
  switch (item.tool) {
    case 'bash':
      return str(a['command']).split('\n')[0] ?? '';
    case 'view_file':
    case 'write_file':
    case 'edit_file':
    case 'list_dir':
      return str(a['path']) || '.';
    case 'grep':
      return `${str(a['pattern'])}${a['path'] ? `  ${str(a['path'])}` : ''}`;
    case 'load_skill':
      return str(a['skill_name']);
    case 'websearch':
      return str(a['topic'] ?? a['query']);
    default: {
      const first = Object.values(a)[0];
      return first === undefined ? '' : str(first);
    }
  }
}

function Pre({ text, className, tone }: { text: string; className?: string; tone?: 'err' }) {
  return (
    <pre
      className={cn(
        'max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-ink/[0.07] bg-code px-3 py-2 font-mono text-[12px] leading-relaxed text-code-ink dark:border-white/[0.08]',
        tone === 'err' && 'border-bad/40 bg-bad/[0.06] text-bad',
        className,
      )}
    >
      {stripControl(text)}
    </pre>
  );
}

function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mt-2.5 first:mt-0">
      <div className="mb-1 flex items-center gap-2 text-2xs font-semibold uppercase tracking-wide text-mute/80">
        <span>{title}</span>
        <span className="ml-auto flex items-center gap-1">{aside}</span>
      </div>
      {children}
    </div>
  );
}

function Diff({ before, after, path }: { before: string; after: string; path?: string }) {
  const lines = diffLines(before, after);
  const { added, removed } = diffStats(lines);
  return (
    <Section
      title={path ? `变更 ${path}` : '变更'}
      aside={
        <span className="font-mono text-[11px] normal-case">
          <span className="text-ok">+{added}</span> <span className="text-bad">-{removed}</span>
        </span>
      }
    >
      <div className="max-h-80 overflow-auto rounded-lg border border-ink/[0.07] bg-code py-1 font-mono text-[12px] leading-[1.6] text-code-ink dark:border-white/[0.08]">
        {lines.map((l, i) => (
          <div
            key={i}
            className={cn(
              'flex whitespace-pre-wrap break-all px-3',
              l.kind === 'add' && 'bg-ok/15',
              l.kind === 'del' && 'bg-bad/15',
            )}
          >
            <span
              className={cn(
                'w-4 shrink-0 select-none',
                l.kind === 'add' ? 'text-ok' : l.kind === 'del' ? 'text-bad' : 'text-code-ink/30',
              )}
            >
              {l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : ' '}
            </span>
            <span className="min-w-0 flex-1">{stripControl(l.text) || ' '}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Args({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args);
  if (!entries.length) return null;
  return (
    <Section
      title="参数"
      aside={<CopyButton text={() => JSON.stringify(args, null, 2)} label="复制参数" />}
    >
      <dl className="space-y-1 text-[12px]">
        {entries.map(([k, v]) => (
          <div key={k} className="flex gap-2">
            <dt className="w-24 shrink-0 truncate font-mono text-mute">{k}</dt>
            <dd className="min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-ink/90">
              {str(v).length > 400 ? `${str(v).slice(0, 400)}…` : str(v)}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

function Output({ item, taskId }: { item: Item; taskId: string }) {
  const [full, setFull] = useState<string | null>(null);
  const load = useMutation({
    mutationFn: () => api.tasks.artifact(taskId, item.artifactId as string),
    onSuccess: (text) => setFull(text),
  });
  const live = item.state === 'running';
  const text = full ?? item.result ?? '';
  const failed = item.state === 'failed';
  const cut = item.truncated || (!!item.artifactId && full === null);

  return (
    <>
      {live && (item.stdout || item.stderr) && (
        <Section title="实时输出">
          {item.stdout && <Pre text={item.stdout} />}
          {item.stderr && <Pre text={item.stderr} tone="err" className="mt-1" />}
        </Section>
      )}
      {!live && (text || item.stdout || item.stderr) && (
        <Section
          title={failed ? '错误' : '输出'}
          aside={<CopyButton text={text || item.stdout + item.stderr} label="复制输出" />}
        >
          <Pre text={text || item.stdout + item.stderr} tone={failed ? 'err' : undefined} />
          {cut && (
            <div className="mt-1 flex items-center gap-2 text-[12px] text-warn">
              <AlertTriangle size={12} />
              <span>已截断{item.bytes ? ` · 共 ${formatBytes(item.bytes)}` : ''}</span>
              {item.artifactId && (
                <button
                  className="rounded px-1.5 py-0.5 font-medium text-accent hover:bg-accent/10 disabled:opacity-50"
                  disabled={load.isPending}
                  onClick={() => load.mutate()}
                >
                  {load.isPending ? '加载中…' : '加载完整输出'}
                </button>
              )}
            </div>
          )}
          {load.isError && (
            <p className="mt-1 text-[12px] text-bad">{(load.error as Error).message}</p>
          )}
        </Section>
      )}
    </>
  );
}

function Detail({ item, taskId }: { item: Item; taskId: string }) {
  const a = item.args;
  const edits = Array.isArray(a['edits']) ? (a['edits'] as Record<string, unknown>[]) : null;
  return (
    <div>
      {item.tool === 'bash' && (
        <Section title="命令" aside={<CopyButton text={str(a['command'])} label="复制命令" />}>
          <Pre text={str(a['command'])} />
        </Section>
      )}
      {item.tool === 'edit_file' && (
        <>
          {edits
            ? edits.map((e, i) => (
                <Diff
                  key={i}
                  before={str(e['old_string'])}
                  after={str(e['new_string'])}
                  path={i === 0 ? str(a['path']) : undefined}
                />
              ))
            : a['old_string'] !== undefined && (
                <Diff
                  before={str(a['old_string'])}
                  after={str(a['new_string'])}
                  path={str(a['path'])}
                />
              )}
        </>
      )}
      {item.tool === 'write_file' && (
        <Section
          title={`写入 ${str(a['path'])}`}
          aside={<CopyButton text={str(a['content'])} label="复制内容" />}
        >
          <Pre
            text={
              str(a['content']).split('\n').slice(0, 60).join('\n') +
              (str(a['content']).split('\n').length > 60 ? '\n…' : '')
            }
          />
        </Section>
      )}
      {item.tool !== 'bash' && item.tool !== 'edit_file' && item.tool !== 'write_file' && (
        <Args args={a} />
      )}
      <Output item={item} taskId={taskId} />
      {item.changedFiles && item.changedFiles.length > 0 && (
        <Section title="影响的文件">
          <ul className="space-y-0.5 font-mono text-[12px]">
            {item.changedFiles.map((f) => (
              <li key={f.path} className="flex gap-2">
                <span className="min-w-0 flex-1 truncate">{f.path}</span>
                {f.added !== undefined && <span className="text-ok">+{f.added}</span>}
                {f.removed !== undefined && <span className="text-bad">-{f.removed}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-mute">
        <span>第 {item.step} 回合</span>
        <span className="tabular-nums">开始 {clock(item.startTs)}</span>
        {item.durationMs !== undefined && (
          <span className="tabular-nums">耗时 {formatDuration(item.durationMs)}</span>
        )}
        {item.exitCode !== undefined && (
          <span className={cn('tabular-nums', item.exitCode !== 0 && 'text-bad')}>
            退出码 {item.exitCode}
          </span>
        )}
        {item.timedOut && <span className="text-bad">超时</span>}
        {item.folded && <span>已在上下文中折叠</span>}
        {item.tool.startsWith('mcp__') && (
          <span className="flex items-center gap-1">
            <Shield size={11} />
            不可信来源
          </span>
        )}
        <span className="font-mono">{item.callId}</span>
      </div>
    </div>
  );
}

export function ToolCard({ item, taskId }: { item: Item; taskId: string }) {
  const running = item.state === 'running';
  const [open, setOpen] = useState(false);
  const Icon = item.tool.startsWith('mcp__') ? Plug : (ICONS[item.tool] ?? Wrench);
  const duration = item.durationMs ?? (item.endTs ? (item.endTs - item.startTs) * 1000 : undefined);
  const expanded = open || (running && item.tool === 'bash' && !!(item.stdout || item.stderr));
  const summary = summaryOf(item);
  const mcpName = item.tool.startsWith('mcp__')
    ? item.tool.split('__').slice(1).join(' · ')
    : item.tool;

  return (
    <div
      className={cn(
        'rounded-xl border bg-card/70 transition-colors',
        item.state === 'failed' ? 'border-bad/30' : 'border-ink/[0.07] dark:border-white/[0.08]',
      )}
    >
      <button
        aria-expanded={expanded}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px]"
      >
        <Icon
          size={15}
          className={cn('shrink-0', item.state === 'failed' ? 'text-bad' : 'text-mute')}
        />
        <span className="shrink-0 font-medium">{mcpName}</span>
        <Tip label={summary} side="top" align="start" disabled={summary.length < 40}>
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-mute">{summary}</span>
        </Tip>
        <span className="flex shrink-0 items-center gap-2 text-[11.5px] tabular-nums text-mute">
          {item.exitCode !== undefined && item.exitCode !== 0 && (
            <span className="text-bad">exit {item.exitCode}</span>
          )}
          {duration !== undefined && !running && <span>{formatDuration(duration)}</span>}
          {running ? (
            <Loader2 size={14} className="animate-spin text-accent" aria-label="运行中" />
          ) : item.state === 'ok' ? (
            <Check size={14} className="text-ok" aria-label="成功" />
          ) : (
            <X size={14} className="text-bad" aria-label="失败" />
          )}
        </span>
        <Chevron open={expanded} size={12} />
      </button>
      <Reveal open={expanded}>
        <div className="border-t border-ink/[0.06] px-3 pb-3 pt-2.5 dark:border-white/[0.06]">
          <Detail item={item} taskId={taskId} />
        </div>
      </Reveal>
    </div>
  );
}
