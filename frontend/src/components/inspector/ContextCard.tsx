/**
 * 右侧第二张卡片：记忆与对话在上下文窗口里的占比。
 * 分母：会话所选模型的 context_window，未选时取各端点里最小的（GET /models）；
 * 分子：按后端同一规则估算 Token（utils/text.ts 的 estimateTokens）。
 * 每一行可展开查看条目，工作区记忆可增删。设计见 docs/10_frontend/03_visual_design.md §7。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { Plus, X } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { contextWindowOf, useModels, useSessionSettings } from '../../api/useSettings';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import type { SessionMemory, WorkspaceMemory } from '../../types/api';
import { cn, estimateTokens, formatTokens } from '../../utils/text';
import { MemoryGlyph } from '../icons/Glyphs';
import { Select } from '../ui/Select';
import { Card, EASE_OUT, IconButton, Reveal } from '../ui/primitives';

interface Row {
  id: string;
  label: string;
  color: string;
  tokens: number;
  entries: string[];
  /** 工作区记忆的层：可删除条目 */
  layer?: keyof WorkspaceMemory;
}

const LAYERS: { key: keyof WorkspaceMemory; label: string; color: string }[] = [
  { key: 'profile', label: '项目概况', color: '#3b82f6' },
  { key: 'guidelines', label: '规范与约定', color: '#f58529' },
  { key: 'architecture', label: '架构', color: '#14b8a6' },
  { key: 'pitfalls', label: '避坑', color: '#eab308' },
];

const sum = (texts: string[]) => texts.reduce((n, t) => n + estimateTokens(t), 0);
const failureText = (f: SessionMemory['failures'][number]) =>
  `${f.action} — ${f.failure_reason} → ${f.conclusion}`;

function sessionRows(m: SessionMemory): Row[] {
  const facts = m.facts.map((f) => f.text);
  const failures = m.failures.map(failureText);
  const pending = [...m.pending_facts.map((f) => f.text), ...m.pending_failures.map(failureText)];
  const summary = m.summary ? [m.summary] : [];
  return [
    {
      id: 's-summary',
      label: '会话摘要',
      color: '#8134af',
      tokens: sum(summary),
      entries: summary,
    },
    { id: 's-facts', label: '事实', color: '#dd2a7b', tokens: sum(facts), entries: facts },
    {
      id: 's-failures',
      label: '失败尝试',
      color: '#ef4444',
      tokens: sum(failures),
      entries: failures,
    },
    { id: 's-pending', label: '待确认', color: '#a8a29e', tokens: sum(pending), entries: pending },
  ];
}

function percent(part: number, whole: number): string {
  if (!whole || !part) return '0%';
  const p = (part / whole) * 100;
  return p < 0.1 ? '<0.1%' : `${p.toFixed(1)}%`;
}

function UsageRow({
  row,
  denominator,
  onRemove,
}: {
  row: Row;
  denominator: number;
  onRemove: (layer: keyof WorkspaceMemory, index: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const expandable = row.entries.length > 0;
  return (
    <li>
      <button
        className={cn(
          '-mx-1.5 flex w-[calc(100%+0.75rem)] items-center gap-2.5 rounded-lg px-1.5 py-[5px] text-left text-[13px]',
          expandable ? 'hover:bg-ink/[0.04]' : 'cursor-default',
        )}
        aria-expanded={expandable ? open : undefined}
        onClick={() => expandable && setOpen((v) => !v)}
      >
        <span className="h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ background: row.color }} />
        <span
          className={cn(
            'min-w-0 flex-1 truncate text-ink/80',
            expandable && 'underline decoration-mute/40 decoration-dotted underline-offset-4',
          )}
        >
          {row.label}
        </span>
        <span className="tabular-nums text-ink/70">{formatTokens(row.tokens)}</span>
        <span className="w-12 text-right tabular-nums text-mute">
          {percent(row.tokens, denominator)}
        </span>
      </button>
      <Reveal open={open}>
        <ul className="mb-1.5 ml-[4px] space-y-1 border-l border-ink/10 pl-[17px] pt-0.5">
          {row.entries.map((text, index) => (
            <li
              key={`${index}-${text}`}
              className="group flex items-start gap-1.5 text-xs leading-snug"
            >
              <span className="line-clamp-4 min-w-0 flex-1 break-words text-mute">{text}</span>
              {row.layer && (
                <button
                  aria-label={`删除 ${text}`}
                  className="text-mute opacity-0 transition-opacity hover:text-bad focus-visible:opacity-100 group-hover:opacity-100"
                  onClick={() => onRemove(row.layer as keyof WorkspaceMemory, index)}
                >
                  <X size={12} />
                </button>
              )}
            </li>
          ))}
        </ul>
      </Reveal>
    </li>
  );
}

export function ContextCard() {
  const workspaceId = useAppStore((s) => s.workspaceId);
  const sessionId = useAppStore((s) => s.sessionId);
  const client = useQueryClient();
  const openCard = useUiStore((s) => s.openCard);
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState('');
  const [layer, setLayer] = useState<keyof WorkspaceMemory>('guidelines');

  const models = useModels();
  const { query: settings } = useSessionSettings(sessionId);
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
  const turns = useQuery({
    queryKey: ['turns', sessionId],
    queryFn: () => api.sessions.turns(sessionId as string, 50),
    enabled: !!sessionId,
  });
  const refresh = () => client.invalidateQueries({ queryKey: ['wmemory', workspaceId] });
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

  const addButton = workspaceId && (
    <IconButton
      label="添加记忆"
      className="h-7 w-7"
      onClick={() => {
        openCard('context'); // 卡片折叠时先展开，表单才看得见
        setAdding((v) => !v);
      }}
    >
      <Plus size={15} />
    </IconButton>
  );

  if (!workspaceId) {
    return (
      <Card id="context" title="上下文占比">
        <div className="flex flex-col items-center py-6 text-center">
          <MemoryGlyph size={20} className="text-mute" />
          <p className="mt-2 text-xs text-mute">选择工作区后显示</p>
        </div>
      </Card>
    );
  }

  const contextWindow = contextWindowOf(models.data, settings.data?.model ?? null);
  const conversation = (turns.data ?? []).map((t) => t.content);
  const rows: Row[] = [
    ...LAYERS.map(({ key, label, color }) => {
      const entries = workspace.data?.[key] ?? [];
      return { id: `w-${key}`, label, color, tokens: sum(entries), entries, layer: key };
    }),
    ...(sessionId && session.data ? sessionRows(session.data) : []),
    ...(sessionId
      ? [
          {
            id: 'turns',
            label: '对话记录',
            color: '#6366f1',
            tokens: sum(conversation),
            entries: [],
          },
        ]
      : []),
  ];
  const used = rows.reduce((n, r) => n + r.tokens, 0);
  const denominator = contextWindow || used;
  const free = Math.max(contextWindow - used, 0);
  const compactAt = settings.data
    ? (settings.data.context_token_limit ?? settings.data.defaults.context_token_limit)
    : null;

  return (
    <Card id="context" title="上下文占比" aside={addButton}>
      <div className="flex items-baseline justify-between">
        <span className="text-[19px] font-semibold tabular-nums tracking-tight">
          {contextWindow ? percent(used, contextWindow) : formatTokens(used)}
          <span className="ml-1 text-xs font-normal text-mute">
            {contextWindow ? '已用' : 'Token'}
          </span>
        </span>
        <span className="text-xs tabular-nums text-mute">
          {formatTokens(used)} / {contextWindow ? formatTokens(contextWindow) : '窗口未知'}
        </span>
      </div>

      {/* 分段条：每段按占比，至少 2px 以便看见 */}
      <div className="mt-2.5 flex h-2 gap-[2px] overflow-hidden rounded-full bg-ink/[0.07]">
        {rows
          .filter((r) => r.tokens > 0)
          .map((r, i) => (
            <motion.span
              key={r.id}
              className="h-full first:rounded-l-full"
              style={{ background: r.color, minWidth: 3 }}
              initial={{ width: 0 }}
              animate={{ width: `${(r.tokens / Math.max(denominator, 1)) * 100}%` }}
              transition={{ duration: 0.5, delay: i * 0.04, ease: EASE_OUT }}
              title={`${r.label}：${r.tokens} Token`}
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
                      className="mt-1 block h-2.5 w-2.5 rounded-[3px]"
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
        {rows.map((row) => (
          <UsageRow
            key={row.id}
            row={row}
            denominator={denominator}
            onRemove={(kind, index) => remove.mutate({ kind, index })}
          />
        ))}
        {contextWindow > 0 && (
          <li className="flex items-center gap-2.5 py-[5px] text-[13px]">
            <span className="h-2.5 w-2.5 shrink-0 rounded-[3px] bg-ink/[0.12]" />
            <span className="flex-1 text-ink/80">剩余空间</span>
            <span className="tabular-nums text-ink/70">{formatTokens(free)}</span>
            <span className="w-12 text-right tabular-nums text-mute">
              {percent(free, contextWindow)}
            </span>
          </li>
        )}
      </ul>

      <p className="mt-2.5 text-2xs leading-relaxed text-mute">
        {compactAt !== null && <>对话超过 {formatTokens(compactAt)} 时压缩 · </>}
        Token 为估算，规则与后端一致
      </p>
    </Card>
  );
}
