/** 模型列表与会话设置：输入台、设置面板、上下文卡片共用同一份缓存。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ModelEndpoint, SessionSettings } from '../types/api';
import { api } from './endpoints';

/** 已装配的端点，按名称去重（同名端点出现在多个档位时取较小的窗口） */
export function useModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: api.system.models,
    staleTime: Infinity,
    select: (rows: ModelEndpoint[]) => {
      const byName = new Map<string, ModelEndpoint>();
      for (const row of Array.isArray(rows) ? rows : []) {
        if (typeof row?.name !== 'string') continue;
        const seen = byName.get(row.name);
        if (!seen || row.context_window < seen.context_window) byName.set(row.name, row);
      }
      return [...byName.values()];
    },
  });
}

export function useSessionSettings(sessionId: string | null) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ['ssettings', sessionId],
    queryFn: () => api.sessions.settings(sessionId as string),
    enabled: !!sessionId,
  });
  const save = useMutation({
    mutationFn: (body: { model: string | null; context_token_limit: number | null }) =>
      api.sessions.saveSettings(sessionId as string, body),
    onSuccess: (data: SessionSettings) => client.setQueryData(['ssettings', sessionId], data),
  });
  return { query, save };
}

/** 上下文窗口：会话选了模型就用它的窗口，否则取各端点里最小的 */
export function contextWindowOf(models: ModelEndpoint[] | undefined, model: string | null): number {
  const list = models ?? [];
  const chosen = model ? list.find((m) => m.name === model) : undefined;
  if (chosen) return chosen.context_window;
  const windows = list.map((m) => m.context_window).filter((n) => n > 0);
  return windows.length ? Math.min(...windows) : 0;
}
