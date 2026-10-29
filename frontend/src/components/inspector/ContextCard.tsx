/**
 * 右侧第二张卡片：下一次请求里各段提示词在上下文窗口中的占比。
 * 数据来自后端检视接口（与真实任务同一套装配），不是前端另外估的：系统提示词、工具定义、
 * 技能清单、项目规则、记忆、对话各有各的份额。设置、系统提示词、记忆、对话任何一项变化都会重新取。
 * 记忆两行可展开，工作区记忆可增删；对话行展开后有一条“压缩线”量表。设计见 03_visual_design.md §7。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { X } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import { useContextSnapshot, useRefreshContext, useSessionSettings } from '../../api/useSettings';
import { useAppStore } from '../../stores/useAppStore';
import type { ContextSegment, SessionMemory, WorkspaceMemory } from '../../types/api';
import { cn, formatTokens } from '../../utils/text';
import { MemoryGlyph } from '../icons/Glyphs';
import { Select } from '../ui/Select';
import { Card, EASE_OUT, Reveal } from '../ui/primitives';

type SegmentId = ContextSegment['id'];

interface Meta {
  label: (s: ContextSegment) => string;
  /** 低饱和的色块：同一把“纸”的色卡，不用高饱和原色 */
  color: string;
  /** 没内容也显示这一行 */
  always?: boolean;
}

const META: Record<SegmentId, Meta> = {
  system: { label: () => '系统提示词', color: '#8f84b8', always: true },
  tools: {
    label: (s) => `工具定义${s.count ? ` · ${s.count} 个` : ''}`,
    color: '#6f9a86',
    always: true,
  },
  skills: { label: () => '技能清单', color: '#c09a56' },
  mcp_index: { label: () => 'MCP 工具索引', color: '#7d9bb5' },
  project_rules: { label: () => '项目规则', color: '#b9806a' },
  workspace: { label: () => '工作区信息', color: '#a39b8b' },
  workspace_memory: { label: () => '工作区记忆', color: '#6c91a6', always: true },
  session_memory: { label: () => '会话记忆', color: '#b57f8f', always: true },
  task_data: { label: () => '环境与近期新增', color: '#c9b27c' },
  history: {
    label: (s) => `对话记录${s.turns ? ` · ${s.turns} 条` : ''}`,
    color: '#6d78a8',
    always: true,
  },
};

const LAYERS: { key: keyof WorkspaceMemory; label: string; color: string }[] = [
  { key: 'profile', label: '项目概况', color: '#6c91a6' },
  { key: 'guidelines', label: '规范与约定', color: '#c09a56' },
  { key: 'architecture', label: '架构', color: '#6f9a86' },
  { key: 'pitfalls', label: '避坑', color: '#b9806a' },
];

function percent(part: number, whole: number): string {
  if (!whole || !part) return '0%';
  const p = (part / whole) * 100;
  return p < 0.1 ? '<0.1%' : `${p.toFixed(1)}%`;
}

const failureText = (f: SessionMemory['failures'][number]) =>
  `${f.action} — ${f.failure_reason} → ${f.conclusion}`;

/** 一行：色块、名称、Token、占比；可展开时整行是按钮 */
function Row({
  color,
  label,
  tokens,
  denominator,
  note,
  open,
  onToggle,
  children,
}: {
  color: string;
  label: string;
  tokens: number;
  denominator: number;
  note?: ReactNode;
  open?: boolean;
  onToggle?: () => void;
  children?: ReactNode;
}) {
  const expandable = !!onToggle;
  return (
    <li>
      <button
        className={cn(
          '-mx-2 flex min-h-8 w-[calc(100%+1rem)] items-center gap-2.5 rounded-base px-2 py-1 text-left text-[13px] transition-colors',
          expandable ? 'hover:bg-ink/[0.045] active:scale-[0.995]' : 'cursor-default',
        )}
        aria-expanded={expandable ? open : undefined}
        onClick={onToggle}
      >
        <span
          className="h-2.5 w-2.5 shrink-0 rounded-micro"
          style={{ background: color, boxShadow: 'inset 0 0 0 1px rgb(0 0 0 / 0.08)' }}
        />
        <span
          className={cn(
            'min-w-0 flex-1 truncate',
            tokens ? 'text-ink/85' : 'text-mute',
            expandable && 'underline decoration-mute/40 decoration-dotted underline-offset-4',
          )}
        >
          {label}
        </span>
        {note}
        <span className={cn('tabular-nums', tokens ? 'text-ink/75' : 'text-mute/70')}>
          {formatTokens(tokens)}
        </span>
        <span className="w-12 text-right tabular-nums text-mute">
          {percent(tokens, denominator)}
        </span>
      </button>
      {expandable && <Reveal open={!!open}>{children}</Reveal>}
    </li>
  );
}

/** 展开后的明细：左侧一条发丝线，条目文字退居幕后 */
function Detail({ children }: { children: ReactNode }) {
  return (
    <div className="mb-1.5 ml-[4px] space-y-2 border-l border-ink/[0.1] pb-0.5 pl-[17px] pt-0.5">
      {children}
    </div>
  );
}

function Entries({
  title,
  color,
  entries,
  onRemove,
}: {
  title?: string;
  color?: string;
  entries: string[];
  onRemove?: (index: number) => void;
}) {
  if (!entries.length && !title) return null;
  return (
    <div>
      {title && (
        <div className="mb-1 flex items-center gap-1.5 text-2xs font-semibold text-mute">
          {color && <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />}
          {title}
        </div>
      )}
      {entries.length ? (
        <ul className="space-y-1">
          {entries.map((text, index) => (
            <li
              key={`${index}-${text}`}
              className="group flex items-start gap-1.5 text-xs leading-snug"
            >
              <span className="line-clamp-4 min-w-0 flex-1 break-words text-mute">{text}</span>
              {onRemove && (
                <button
                  aria-label={`删除 ${text}`}
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-mute opacity-0 transition-opacity hover:bg-ink/[0.06] hover:text-bad focus-visible:opacity-100 group-hover:opacity-100"
                  onClick={() => onRemove(index)}
                >
                  <X size={12} />
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-2xs text-mute/70">暂无</p>
      )}
    </div>
  );
}

/** 一条细量表：值相对于某条“线” */
function Gauge({ value, limit, label }: { value: number; limit: number; label: string }) {
  const ratio = limit ? Math.min(value / limit, 1) : 0;
  const over = limit > 0 && value >= limit;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-2xs text-mute">
        <span>{label}</span>
        <span className="tabular-nums">
          {formatTokens(value)} / {limit ? formatTokens(limit) : '—'}
        </span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={limit}
        className="h-[3px] overflow-hidden rounded-full bg-ink/[0.08]"
      >
        <motion.div
          className={cn('h-full rounded-full', over ? 'bg-warn' : 'bg-ink/40')}
          initial={{ width: 0 }}
          animate={{ width: `${ratio * 100}%` }}
          transition={{ duration: 0.5, ease: EASE_OUT }}
        />
      </div>
    </div>
  );
}

function Skeleton() {
  return (
    <div aria-label="读取中" className="space-y-2.5 pt-1">
      <div className="h-6 w-28 animate-pulse rounded-base bg-ink/[0.06]" />
      <div className="h-2 animate-pulse rounded-full bg-ink/[0.06]" />
      {[70, 55, 80, 45, 60].map((w, i) => (
        <div
          key={i}
          className="h-3.5 animate-pulse rounded-full bg-ink/[0.05]"
          style={{ width: `${w}%`, animationDelay: `${i * 0.08}s` }}
        />
      ))}
    </div>
  );
}

export function ContextCard() {
  const workspaceId = useAppStore((s) => s.workspaceId);
  const sessionId = useAppStore((s) => s.sessionId);
  const client = useQueryClient();
  const refreshContext = useRefreshContext();
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState('');
  const [layer, setLayer] = useState<keyof WorkspaceMemory>('guidelines');
  const [expanded, setExpanded] = useState<SegmentId | null>(null);

  const { query: settings } = useSessionSettings(sessionId);
  const snapshot = useContextSnapshot(sessionId);
  const workspace = useQuery({
    queryKey: ['wmemory', workspaceId],
    queryFn: () => api.workspaces.memory(workspaceId as string),
    enabled: !!workspaceId,
  });
  const session = useQuery({
    queryKey: ['smemory', sessionId],
    queryFn: () => api.sessions.memory(sessionId as string),
    enabled: !!sessionId,
  });

  const refresh = async () => {
    await client.invalidateQueries({ queryKey: ['wmemory', workspaceId] });
    await refreshContext();
  };
  const add = useMutation({
    mutationFn: () => api.workspaces.addFact(workspaceId as string, text.trim(), layer),
    onSuccess: async () => {
      setText('');
      setAdding(false);
      await refresh();
    },
  });
  const remove = useMutation({
    mutationFn: ({ kind, index }: { kind: keyof WorkspaceMemory; index: number }) =>
      api.workspaces.removeMemory(workspaceId as string, kind, index),
    onSuccess: refresh,
  });

  if (!workspaceId || !sessionId) {
    return (
      <Card id="context" title="上下文占比">
        <div className="flex flex-col items-center py-6 text-center">
          <MemoryGlyph size={20} className="text-mute" />
          <p className="mt-2 text-xs text-mute">选择一个会话后显示</p>
        </div>
      </Card>
    );
  }

  const data = snapshot.data;
  const compactAt = settings.data
    ? (settings.data.context_token_limit ?? settings.data.defaults.context_token_limit)
    : 0;
  const maxContext = compactAt > 0 ? compactAt : (data?.context_window ?? 0);
  const used = data?.total_tokens ?? 0;
  const denominator = maxContext > 0 ? Math.max(maxContext, used) : used || 1;
  const rows = (data?.segments ?? []).filter((s) => s.tokens > 0 || META[s.id].always);
  const free = Math.max(maxContext - used, 0);
  const toggle = (id: SegmentId) => setExpanded((cur) => (cur === id ? null : id));

  const memory = session.data;
  const sessionEntries = memory
    ? {
        summary: memory.summary ? [memory.summary] : [],
        facts: memory.facts.map((f) => f.text),
        failures: memory.failures.map(failureText),
        pending: [
          ...memory.pending_facts.map((f) => f.text),
          ...memory.pending_failures.map(failureText),
        ],
      }
    : null;

  return (
    <Card id="context" title="上下文占比">
      {snapshot.isError ? (
        <p role="alert" className="py-3 text-xs leading-relaxed text-mute">
          读取上下文占比失败：{(snapshot.error as Error).message}
          <button className="ml-2 font-semibold text-ink hover:underline" onClick={refreshContext}>
            重试
          </button>
        </p>
      ) : !data ? (
        <Skeleton />
      ) : (
        <>
          <div className="flex items-baseline justify-between">
            <span className="text-[19px] font-semibold tabular-nums tracking-tight">
              {maxContext ? percent(used, maxContext) : formatTokens(used)}
              <span className="ml-1 text-xs font-normal text-mute">已用</span>
            </span>
            <span className="text-xs tabular-nums text-mute">
              {formatTokens(used)} / {maxContext ? formatTokens(maxContext) : '上限未知'}
            </span>
          </div>

          {/* 分段条：每段按占比，至少 3px 以便看见；发丝边的凹槽 */}
          <div
            className="mt-2.5 flex h-2 gap-[2px] overflow-hidden rounded-full bg-ink/[0.06] p-[1px]"
            style={{ boxShadow: 'inset 0 1px 1.5px rgb(40 32 20 / 0.08)' }}
          >
            {rows
              .filter((r) => r.tokens > 0)
              .map((r, i) => (
                <motion.span
                  key={r.id}
                  className="h-full rounded-full"
                  style={{ background: META[r.id].color, minWidth: 3 }}
                  initial={{ width: 0 }}
                  animate={{ width: `${(r.tokens / Math.max(denominator, 1)) * 100}%` }}
                  transition={{ duration: 0.5, delay: i * 0.04, ease: EASE_OUT }}
                  title={`${META[r.id].label(r)}：${r.tokens} Token`}
                />
              ))}
          </div>

          <AnimatePresence initial={false}>
            {adding && (
              <motion.form
                key="add"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="overflow-visible"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (text.trim()) add.mutate();
                }}
              >
                <div className="mt-3 flex items-center gap-1.5">
                  <Select
                    label="记忆分类"
                    value={layer}
                    onChange={setLayer}
                    options={LAYERS.map((l) => ({
                      value: l.key,
                      label: l.label,
                      icon: (
                        <span
                          className="mt-1 block h-2.5 w-2.5 rounded-micro"
                          style={{ background: l.color }}
                        />
                      ),
                    }))}
                  />
                  <input
                    aria-label="新增记忆"
                    autoFocus
                    className="field min-w-0 flex-1 py-1.5 text-xs"
                    placeholder="要长期记住的事实，回车添加"
                    maxLength={2000}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                  />
                </div>
              </motion.form>
            )}
          </AnimatePresence>

          <ul className="mt-3">
            {rows.map((seg) => {
              const meta = META[seg.id];
              const common = {
                color: meta.color,
                label: meta.label(seg),
                tokens: seg.tokens,
                denominator,
                open: expanded === seg.id,
              };
              if (seg.id === 'system') {
                return (
                  <Row
                    key={seg.id}
                    {...common}
                    note={seg.customized ? <span className="stamp">已自定义</span> : undefined}
                  />
                );
              }
              if (seg.id === 'workspace_memory') {
                return (
                  <Row key={seg.id} {...common} onToggle={() => toggle(seg.id)}>
                    <Detail>
                      {LAYERS.map((l) => (
                        <Entries
                          key={l.key}
                          title={l.label}
                          color={l.color}
                          entries={workspace.data?.[l.key] ?? []}
                          onRemove={(index) => remove.mutate({ kind: l.key, index })}
                        />
                      ))}
                    </Detail>
                  </Row>
                );
              }
              if (seg.id === 'session_memory') {
                return (
                  <Row key={seg.id} {...common} onToggle={() => toggle(seg.id)}>
                    <Detail>
                      <Entries title="会话摘要" entries={sessionEntries?.summary ?? []} />
                      <Entries title="事实" entries={sessionEntries?.facts ?? []} />
                      <Entries title="失败尝试" entries={sessionEntries?.failures ?? []} />
                      <Entries title="待确认" entries={sessionEntries?.pending ?? []} />
                    </Detail>
                  </Row>
                );
              }
              if (seg.id === 'history') {
                return (
                  <Row key={seg.id} {...common} onToggle={() => toggle(seg.id)}>
                    <Detail>
                      <Gauge
                        value={seg.tokens}
                        limit={compactAt}
                        label="距压缩线（超过后较早的对话并入会话记忆）"
                      />
                    </Detail>
                  </Row>
                );
              }
              return <Row key={seg.id} {...common} />;
            })}
            {maxContext > 0 && (
              <li className="flex min-h-8 items-center gap-2.5 py-1 text-[13px]">
                <span className="h-2.5 w-2.5 shrink-0 rounded-micro bg-ink/[0.1]" />
                <span className="flex-1 text-ink/80">剩余空间</span>
                <span className="tabular-nums text-ink/75">{formatTokens(free)}</span>
                <span className="w-12 text-right tabular-nums text-mute">
                  {percent(free, maxContext)}
                </span>
              </li>
            )}
          </ul>
        </>
      )}
    </Card>
  );
}
