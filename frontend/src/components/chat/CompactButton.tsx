/**
 * 输入台里的“压缩记忆”：把较早的对话并入会话摘要，只保留最近几轮原文。
 * 原始对话不会被删除，只是不再整段送进模型。对应 POST /sessions/{id}/memory/compact。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, Shrink } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import type { CompactionResult } from '../../types/api';
import { cn } from '../../utils/text';

const RESULT_VISIBLE_MS = 3500;

export function resultLabel(result: CompactionResult): string {
  return result.status === 'compacted'
    ? result.turns > 0
      ? `已压缩 ${result.turns} 条`
      : '已整理记忆'
    : '没有可压缩的内容';
}

export function CompactButton({ sessionId, disabled }: { sessionId: string; disabled?: boolean }) {
  const client = useQueryClient();
  const [shown, setShown] = useState<CompactionResult | null>(null);
  const compact = useMutation({
    mutationFn: () => api.sessions.compact(sessionId),
    onSuccess: async (result) => {
      setShown(result);
      await client.invalidateQueries({ queryKey: ['smemory', sessionId] });
      await client.invalidateQueries({ queryKey: ['context', sessionId] });
    },
    onError: (error) => {
      const e = error as { message?: string; traceId?: string };
      useAppStore.setState({
        notice: { message: e.message ?? String(error), traceId: e.traceId || undefined },
      });
    },
  });

  useEffect(() => {
    if (!shown) return;
    const timer = setTimeout(() => setShown(null), RESULT_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [shown]);
  useEffect(() => setShown(null), [sessionId]);

  const pending = compact.isPending;
  const Icon = pending ? Loader2 : shown ? Check : Shrink;
  const label = pending ? '压缩中…' : shown ? resultLabel(shown) : '压缩记忆';
  return (
    <button
      type="button"
      aria-label="压缩记忆"
      title="把较早的对话并入摘要，只保留最近几轮原文；原始对话不会被删除"
      disabled={disabled || pending}
      onClick={() => compact.mutate()}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors',
        'bg-ink/[0.045] text-ink hover:bg-ink/[0.08] disabled:cursor-not-allowed disabled:opacity-50',
        shown && 'bg-ok/10 text-ok hover:bg-ok/10',
      )}
    >
      <Icon size={13} className={cn(pending && 'animate-spin')} aria-hidden />
      <span role="status" aria-live="polite">
        {label}
      </span>
    </button>
  );
}
