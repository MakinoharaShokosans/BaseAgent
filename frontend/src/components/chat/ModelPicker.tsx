/**
 * 输入台里的模型下拉：选项只来自 GET /models，选择立即写入会话设置（刷新后保留，
 * 与设置面板里的同一项一致）。设计见 docs/10_frontend/03_visual_design.md §6.1。
 */
import { useModels, useSessionSettings } from '../../api/useSettings';
import { formatTokens } from '../../utils/text';
import { Select } from '../ui/Select';

const AUTO = '__auto__';

export function ModelPicker({ sessionId }: { sessionId: string }) {
  const models = useModels();
  const { query, save } = useSessionSettings(sessionId);
  const current = query.data?.model ?? AUTO;
  const list = models.data ?? [];
  return (
    <Select
      label="模型"
      value={current}
      placement="up"
      disabled={!query.data || save.isPending}
      leading={<span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-brand" />}
      onChange={(value) =>
        save.mutate({
          model: value === AUTO ? null : value,
          context_token_limit: query.data?.context_token_limit ?? null,
        })
      }
      options={[
        { value: AUTO, label: '自动', hint: '按配置的端点顺序，失败时自动降级' },
        ...list.map((m) => ({
          value: m.name,
          label: m.name,
          hint: `${m.model} · ${formatTokens(m.context_window)} 上下文`,
        })),
      ]}
    />
  );
}
