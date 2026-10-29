import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../../api/endpoints';
import { ConfirmDialog } from '../../../components/modals/ConfirmDialog';
import { IconButton, Reveal, Chevron } from '../../../components/ui/primitives';
import { Select } from '../../../components/ui/Select';
import { Toggle } from '../../../components/ui/Toggle';
import { useAppStore } from '../../../stores/useAppStore';
import { usePendingStore } from '../../../stores/usePendingStore';
import type { McpServer } from '../../../types/api';
import { cn } from '../../../utils/text';
import { Group, Scope, UNSUPPORTED, supportedBy } from '../parts';

const STATUS: Record<McpServer['status'], { label: string; dot: string }> = {
  connected: { label: '在线', dot: 'bg-ok' },
  idle: { label: '待连接', dot: 'bg-mute/50' },
  failed: { label: '失败', dot: 'bg-bad' },
  disabled: { label: '已关闭', dot: 'border border-mute/50' },
};

function Server({ server, writable }: { server: McpServer; writable: boolean }) {
  const client = useQueryClient();
  const notify = useAppStore((s) => s.notify);
  const mark = usePendingStore((s) => s.mark);
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const enabled = server.enabled ?? server.status !== 'disabled';
  const disabledTools = server.disabled_tools ?? [];
  const status = STATUS[server.status] ?? STATUS.idle;

  const update = useMutation({
    mutationFn: (body: Parameters<typeof api.mcp.update>[1]) => api.mcp.update(server.name, body),
    onSuccess: () => {
      mark();
      return client.invalidateQueries({ queryKey: ['mcp-servers'] });
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const reconnect = useMutation({
    mutationFn: () => api.mcp.reconnect(server.name),
    onSuccess: () => client.invalidateQueries({ queryKey: ['mcp-servers'] }),
    onError: (e: Error) => notify(e.message, 'error'),
  });

  return (
    <div className="py-3">
      <div className="flex items-center gap-2.5">
        <button
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
        >
          <span
            className={cn('h-2.5 w-2.5 shrink-0 rounded-full', status.dot)}
            aria-label={status.label}
            role="img"
          />
          <span className="font-medium">{server.name}</span>
          <Scope>{server.transport}</Scope>
          <span className="text-xs tabular-nums text-mute">{server.tool_count}</span>
          <Chevron open={open} size={12} />
        </button>
        {server.status === 'failed' && (
          <IconButton
            label={server.retry_in_sec ? `立即重试（${server.retry_in_sec}s 后自动）` : '重试'}
            disabled={!writable || reconnect.isPending}
            onClick={() => reconnect.mutate()}
          >
            <RefreshCw size={14} className={cn(reconnect.isPending && 'animate-spin')} />
          </IconButton>
        )}
        <Toggle
          label={`启用 ${server.name}`}
          checked={enabled}
          disabled={!writable || update.isPending}
          tip={writable ? undefined : UNSUPPORTED}
          onChange={(next) => (next ? setConfirming(true) : update.mutate({ enabled: false }))}
        />
      </div>
      <Reveal open={open}>
        <div className="mt-3 space-y-3 pl-5 text-[13px]">
          {server.last_error && <p className="break-words text-bad">{server.last_error}</p>}
          <div className="flex items-center gap-3">
            <span className="w-16 shrink-0 text-mute">暴露</span>
            <Select
              label="暴露方式"
              disabled={!writable}
              title={writable ? undefined : UNSUPPORTED}
              value={server.exposure ?? 'deferred'}
              onChange={(exposure) => update.mutate({ exposure })}
              options={[
                {
                  value: 'deferred',
                  label: '按需',
                  hint: '只列索引，用到时再取回完整定义，省上下文',
                },
                { value: 'pinned', label: '常驻', hint: '完整定义常驻上下文' },
              ]}
            />
          </div>
          {server.tools.length > 0 && (
            <ul className="space-y-1">
              {server.tools.map((t) => {
                const off = disabledTools.includes(t);
                return (
                  <li key={t} className="flex items-center gap-2">
                    <span
                      className={cn(
                        'min-w-0 flex-1 truncate font-mono text-[12.5px]',
                        off && 'text-mute line-through',
                      )}
                    >
                      {t}
                    </span>
                    <Toggle
                      label={`启用工具 ${t}`}
                      checked={!off}
                      disabled={!writable || update.isPending}
                      tip={writable ? undefined : UNSUPPORTED}
                      onChange={(next) =>
                        update.mutate({
                          disabled_tools: next
                            ? disabledTools.filter((x) => x !== t)
                            : [...disabledTools, t],
                        })
                      }
                    />
                  </li>
                );
              })}
            </ul>
          )}
          {server.rejected.length > 0 && (
            <ul className="space-y-0.5 text-warn">
              {server.rejected.map((r) => (
                <li key={r.tool} className="break-words text-[12.5px]">
                  <span className="font-mono">{r.tool}</span> {r.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Reveal>
      {confirming && (
        <ConfirmDialog
          title={`启用 ${server.name}`}
          confirmLabel="启用"
          onClose={() => setConfirming(false)}
          onConfirm={() => update.mutateAsync({ enabled: true })}
        >
          将运行外部进程，其返回内容一律按不可信处理。
          {server.command_preview && (
            <pre className="mt-2 overflow-auto rounded-lg bg-code p-2.5 font-mono text-[12px] text-code-ink">
              {server.command_preview}
            </pre>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}

export function Mcp() {
  const client = useQueryClient();
  const notify = useAppStore((s) => s.notify);
  const mark = usePendingStore((s) => s.mark);
  const servers = useQuery({
    queryKey: ['mcp-servers'],
    queryFn: api.mcp.servers,
    refetchInterval: 8000,
  });
  const master = useQuery({ queryKey: ['mcp-settings'], queryFn: api.mcp.settings, retry: false });
  const toggleMaster = useMutation({
    mutationFn: (enabled: boolean) => api.mcp.setEnabledAll(enabled),
    onSuccess: () => {
      mark();
      void client.invalidateQueries({ queryKey: ['mcp-servers'] });
      return client.invalidateQueries({ queryKey: ['mcp-settings'] });
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const writable = supportedBy(master) && (servers.data ?? []).some((s) => s.enabled !== undefined);

  return (
    <Group
      title="服务器"
      aside={
        <span className="flex items-center gap-2">
          <span className="text-2xs text-mute">总开关</span>
          <Toggle
            label="MCP 总开关"
            checked={master.data?.enabled ?? true}
            disabled={!supportedBy(master) || toggleMaster.isPending}
            tip={supportedBy(master) ? 'MCP 总开关' : UNSUPPORTED}
            onChange={(v) => toggleMaster.mutate(v)}
          />
        </span>
      }
    >
      {(servers.data ?? []).length === 0 && <div className="py-3 text-[13px] text-mute">—</div>}
      {(servers.data ?? []).map((s) => (
        <Server key={s.name} server={s} writable={writable} />
      ))}
      {servers.isError && (
        <p className="py-3 text-sm text-bad">{(servers.error as Error).message}</p>
      )}
    </Group>
  );
}
