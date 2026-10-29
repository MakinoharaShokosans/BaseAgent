/** 上下文占比：下一次请求里各段提示词的份额（后端检视接口，与真实任务同一套装配）。 */
import { AnimatePresence, motion } from 'motion/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Eye, Loader2, Shrink } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, isUnsupported } from '../../api/endpoints';
import { useContextSnapshot, useRefreshContext, useSessionSettings } from '../../api/useSettings';
import { Card, EASE_OUT, IconButton, Modal } from '../../components/ui/primitives';
import { Slider } from '../../components/ui/Slider';
import { useAppStore } from '../../stores/useAppStore';
import type { ContextSegment } from '../../types/api';
import { TERMINAL_STATUSES } from '../../types/api';
import { cn, formatTokens } from '../../utils/text';

type SegmentId = ContextSegment['id'];

const META: Record<
  SegmentId,
  { label: (s: ContextSegment) => string; color: string; always?: boolean }
> = {
  system: { label: () => '系统提示词', color: '#8f84b8', always: true },
  tools: {
    label: (s) => `工具定义${s.count ? ` · ${s.count}` : ''}`,
    color: '#6f9a86',
    always: true,
  },
  skills: { label: () => '技能清单', color: '#c09a56' },
  mcp_index: { label: () => 'MCP 索引', color: '#7d9bb5' },
  project_rules: { label: () => '项目规则', color: '#b9806a' },
  workspace: { label: () => '工作区信息', color: '#a39b8b' },
  workspace_memory: { label: () => '工作区记忆', color: '#6c91a6', always: true },
  session_memory: { label: () => '会话记忆', color: '#b57f8f', always: true },
  task_data: { label: () => '环境与近期新增', color: '#c9b27c' },
  history: {
    label: (s) => `对话记录${s.turns ? ` · ${s.turns}` : ''}`,
    color: '#6d78a8',
    always: true,
  },
};

function percent(part: number, whole: number): string {
  if (!whole || !part) return '0%';
  const p = (part / whole) * 100;
  return p < 0.1 ? '<0.1%' : `${p.toFixed(1)}%`;
}

/** 后端新增分段时不至于整张卡崩掉 */
function metaOf(id: string) {
  return META[id as SegmentId] ?? { label: () => id, color: '#8a8a8a', always: false };
}

function SegmentText({
  sessionId,
  segment,
  onClose,
}: {
  sessionId: string;
  segment: ContextSegment;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ['segment', sessionId, segment.id],
    queryFn: () => api.sessions.contextSegment(sessionId, segment.id),
    retry: false,
  });
  return (
    <Modal title={metaOf(segment.id).label(segment)} onClose={onClose} wide>
      {q.isPending && <Loader2 className="mx-auto animate-spin text-mute" />}
      {q.isError && (
        <p className="text-sm text-mute">
          {isUnsupported(q.error) ? '后端未提供' : (q.error as Error).message}
        </p>
      )}
      {q.data && (
        <>
          <p className="mb-2 text-xs tabular-nums text-mute">{formatTokens(q.data.tokens)} Token</p>
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
            {q.data.text}
          </pre>
        </>
      )}
    </Modal>
  );
}

export function ContextCard({ sessionId }: { sessionId: string }) {
  const client = useQueryClient();
  const refresh = useRefreshContext();
  const compactions = useAppStore((s) =>
    s.runSession === sessionId ? (s.run?.compactions ?? 0) : 0,
  );
  const notify = useAppStore((s) => s.notify);
  const live = useAppStore(
    (s) => s.runSession === sessionId && !!s.run && !TERMINAL_STATUSES.includes(s.run.status),
  );
  const { query: settings, save } = useSessionSettings(sessionId);
  const snapshot = useContextSnapshot(sessionId);
  const [viewing, setViewing] = useState<ContextSegment | null>(null);
  const compact = useMutation({
    mutationFn: () => api.sessions.compact(sessionId),
    onSuccess: async (r) => {
      notify(r.status === 'compacted' ? `已压缩 ${r.turns} 条` : '没有可压缩的内容');
      await client.invalidateQueries({ queryKey: ['smemory', sessionId] });
      await refresh();
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const data = snapshot.data;
  const s = settings.data;
  const compactAt = s ? (s.context_token_limit ?? s.defaults.context_token_limit) : 0;
  // 拖动时不逐像素发 PUT：先改本地值，停手后合并成一次保存
  const [draftLimit, setDraftLimit] = useState<number | null>(null);
  const shownLimit = draftLimit ?? compactAt;
  useEffect(() => {
    if (draftLimit === null || !s || !save.mutate) return;
    const timer = setTimeout(() => {
      save.mutate({ model: s.model, context_token_limit: draftLimit });
      setDraftLimit(null);
    }, 420);
    return () => clearTimeout(timer);
  }, [draftLimit, s, save]);
  const window_ = data?.context_window ?? 0;
  const maxContext = shownLimit > 0 ? shownLimit : window_;
  const used = data?.total_tokens ?? 0;
  const denominator = maxContext > 0 ? Math.max(maxContext, used) : used || 1;
  const rows = (data?.segments ?? []).filter((r) => r.tokens > 0 || metaOf(r.id).always);

  return (
    <Card
      id="context"
      title="上下文"
      aside={
        <IconButton
          label={live ? '任务运行中，结束后才能压缩记忆' : '压缩记忆'}
          className="h-7 w-7"
          disabled={compact.isPending || live}
          onClick={() => compact.mutate()}
        >
          {compact.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Shrink size={14} />
          )}
        </IconButton>
      }
    >
      {snapshot.isError ? (
        <p role="alert" className="py-2 text-xs text-mute">
          {(snapshot.error as Error).message}
          <button className="ml-2 font-semibold text-ink hover:underline" onClick={refresh}>
            重试
          </button>
        </p>
      ) : !data ? (
        <div aria-label="读取中" className="space-y-2 pt-1">
          <div className="h-6 w-28 animate-pulse rounded-base bg-ink/[0.06]" />
          <div className="h-2 animate-pulse rounded-full bg-ink/[0.06]" />
        </div>
      ) : (
        <>
          <div className="flex items-baseline justify-between">
            <span className="text-[19px] font-semibold tabular-nums tracking-tight">
              {maxContext ? percent(used, maxContext) : formatTokens(used)}
            </span>
            <span className="text-xs tabular-nums text-mute">
              {formatTokens(used)} / {maxContext ? formatTokens(maxContext) : '—'}
            </span>
          </div>
          <div className="mt-2.5 flex h-2 gap-[2px] overflow-hidden rounded-full bg-ink/[0.06] p-[1px]">
            {rows
              .filter((r) => r.tokens > 0)
              .map((r, i) => (
                <motion.span
                  key={r.id}
                  className="h-full rounded-full"
                  style={{ background: metaOf(r.id).color, minWidth: 3 }}
                  initial={{ width: 0 }}
                  animate={{ width: `${(r.tokens / Math.max(denominator, 1)) * 100}%` }}
                  transition={{ duration: 0.5, delay: i * 0.04, ease: EASE_OUT }}
                />
              ))}
          </div>
          <ul className="mt-3">
            {rows.map((seg) => (
              <li key={seg.id}>
                <button
                  onClick={() => setViewing(seg)}
                  className="group -mx-2 flex min-h-8 w-[calc(100%+1rem)] items-center gap-2.5 rounded-base px-2 py-1 text-left text-[13px] transition-colors hover:bg-ink/[0.045]"
                >
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-micro"
                    style={{ background: metaOf(seg.id).color }}
                  />
                  <span
                    className={cn(
                      'min-w-0 flex-1 truncate',
                      seg.tokens ? 'text-ink/85' : 'text-mute',
                    )}
                  >
                    {metaOf(seg.id).label(seg)}
                  </span>
                  {seg.customized && <span className="stamp">自定义</span>}
                  <Eye
                    size={12}
                    className="shrink-0 text-mute opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
                  />
                  <span className="tabular-nums text-ink/75">{formatTokens(seg.tokens)}</span>
                  <span className="w-12 text-right tabular-nums text-mute">
                    {percent(seg.tokens, denominator)}
                  </span>
                </button>
              </li>
            ))}
            {data.skipped.length > 0 && (
              <li className="px-0 py-1 text-xs text-warn">跳过：{data.skipped.join('、')}</li>
            )}
          </ul>
          {s && (
            <div className="mt-3 border-t border-ink/[0.06] pt-3">
              <div className="mb-1 flex items-center justify-between text-2xs text-mute">
                <span>压缩线</span>
                <span className="tabular-nums">{formatTokens(shownLimit)}</span>
              </div>
              <Slider
                label="压缩线"
                value={shownLimit}
                min={s.bounds.context_token_limit.min}
                max={Math.max(
                  s.bounds.context_token_limit.max,
                  s.bounds.context_token_limit.min + 1,
                )}
                step={s.bounds.context_token_limit.step}
                scale="log"
                format={formatTokens}
                disabled={save.isPending}
                onChange={setDraftLimit}
              />
              {compactions > 0 && (
                <p className="mt-1.5 text-2xs text-mute">本任务已压缩 {compactions} 次</p>
              )}
            </div>
          )}
        </>
      )}
      <AnimatePresence>
        {viewing && (
          <SegmentText sessionId={sessionId} segment={viewing} onClose={() => setViewing(null)} />
        )}
      </AnimatePresence>
    </Card>
  );
}
