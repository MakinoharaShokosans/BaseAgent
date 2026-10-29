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
    onSuccess: (data: SessionSettings) => {
      client.setQueryData(['ssettings', sessionId], data);
      void client.invalidateQueries({ queryKey: ['context'] });
    },
  });
  return { query, save };
}

/** 会话下一次请求的各段占比。设置、系统提示词、记忆、对话任何一项变化后都要让它失效。 */
export function useContextSnapshot(sessionId: string | null) {
  return useQuery({
    queryKey: ['context', sessionId],
    queryFn: () => api.sessions.context(sessionId as string),
    enabled: !!sessionId,
    retry: false,
  });
}

/** 让上下文占比重新取一次（所有会话；变化的来源不止一个会话，也包括全局系统提示词） */
export function useRefreshContext() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: ['context'] });
}
