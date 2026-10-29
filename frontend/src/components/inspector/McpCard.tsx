/**
 * 右侧卡片：已配置的 MCP 外部工具服务器及其状态。只读，数据来自 GET /mcp/servers
 * （不含命令行与环境变量）。启用与否在 config.toml 的 [mcp.servers.*] 里改，设计见 docs/04_tools/04_mcp.md。
 */
import { useQuery } from '@tanstack/react-query';
import { Plug } from 'lucide-react';
import { api } from '../../api/endpoints';
import type { McpServer } from '../../types/api';
import { Card, StatusPill } from '../ui/primitives';

const STATUS: Record<McpServer['status'], { tone: 'green' | 'red' | 'slate'; label: string }> = {
  connected: { tone: 'green', label: '已连接' },
  failed: { tone: 'red', label: '连接失败' },
  idle: { tone: 'slate', label: '待连接' },
  disabled: { tone: 'slate', label: '未启用' },
};

function detail(s: McpServer): string | null {
  if (s.status === 'failed')
    return `${s.last_error}${s.retry_in_sec ? `（${s.retry_in_sec} 秒后重试）` : ''}`;
  if (s.status === 'connected') {
    const rejected = s.rejected.length ? `，${s.rejected.length} 个被拒绝` : '';
    return `${s.tool_count} 个工具${rejected}`;
  }
  return s.last_error || null;
}

export function McpCard() {
  const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.system.mcpServers });
  const list = Array.isArray(servers.data) ? servers.data : [];
  const enabled = list.filter((s) => s.status !== 'disabled');
  return (
    <Card id="mcp" title="MCP 服务器" icon={<Plug size={15} />}>
      {enabled.length === 0 ? (
        <div className="flex flex-col items-center py-5 text-center">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-ink/[0.04] text-mute mb-1">
            <Plug size={15} />
          </div>
          <p className="mt-1 text-xs text-mute leading-relaxed max-w-[240px]">
            没有启用的服务器。在 config.toml 的 [mcp.servers.*] 中配置。
          </p>
        </div>
      ) : (
        <ul className="space-y-2.5">
          {enabled.map((s) => (
            <li key={s.name} className="min-w-0">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-[13px] font-semibold">{s.name}</span>
                <StatusPill tone={STATUS[s.status].tone}>{STATUS[s.status].label}</StatusPill>
              </div>
              {detail(s) && (
                <p
                  className="mt-0.5 break-words text-xs text-mute"
                  title={s.rejected.map((r) => `${r.tool}：${r.reason}`).join('\n') || undefined}
                >
                  {detail(s)}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
