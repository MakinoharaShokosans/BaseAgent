/** 顶栏：面包屑、状态胶囊、事件流连接、检视器开关。见 03_visual_design.md §5.2。 */
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { Menu, PanelRight, WifiOff } from 'lucide-react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import { CONNECTION_LABEL, connectionHealthy, statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { IconButton, StatusPill } from '../ui/primitives';

export function TopBar({ onOpenNav }: { onOpenNav?: () => void }) {
  const workspaceId = useAppStore((s) => s.workspaceId);
  const sessionId = useAppStore((s) => s.sessionId);
  const run = useAppStore((s) => s.run);
  const connection = useAppStore((s) => s.connection);
  const inspectorOpen = useUiStore((s) => (s.narrow ? s.drawerOpen : s.inspectorOpen));
  const toggleInspector = useUiStore((s) => s.toggleInspector);

  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.workspaces.list });
  const sessions = useQuery({
    queryKey: ['sessions', workspaceId],
    queryFn: () => api.sessions.list(workspaceId as string),
    enabled: !!workspaceId,
  });
  const workspace = workspaces.data?.find((w) => w.workspace_id === workspaceId);
  const index = sessions.data?.findIndex((s) => s.session_id === sessionId) ?? -1;
  const sessionLabel = index >= 0 && sessions.data ? `会话 ${sessions.data.length - index}` : null;
  const meta = run ? statusMeta(run.status) : null;

  return (
    <header className="relative z-10 flex h-14 shrink-0 items-center gap-3 px-5">
      {onOpenNav && (
        <IconButton label="打开导航" onClick={onOpenNav}>
          <Menu size={17} />
        </IconButton>
      )}
      <nav aria-label="位置" className="flex min-w-0 items-center gap-1.5 text-[13px]">
        {workspace ? (
          <span className="truncate font-semibold" title={workspace.root_path}>
            {workspace.name}
          </span>
        ) : (
          <span className="text-mute">未选择工作区</span>
        )}
        {sessionLabel && (
          <>
            <span className="text-mute/50">/</span>
            <span className="truncate text-mute">{sessionLabel}</span>
          </>
        )}
      </nav>

      <div className="ml-auto flex items-center gap-2">
        <AnimatePresence mode="popLayout" initial={false}>
          {run && meta && (
            <motion.div
              key={run.status}
              initial={{ opacity: 0, y: -6, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.96 }}
              transition={{ duration: 0.22 }}
              className="flex items-center gap-2"
            >
              <StatusPill tone={meta.tone} icon={meta.icon} live={meta.live}>
                {meta.label}
              </StatusPill>
              <span className="hidden text-xs tabular-nums text-mute md:inline">
                {run.stepCount} 步 · {formatTokens(run.totalTokens)} Token
              </span>
            </motion.div>
          )}
        </AnimatePresence>
        {!connectionHealthy(connection) && (
          <span
            role="status"
            className={cn(
              'flex items-center gap-1.5 rounded-full bg-warn/12 px-2.5 py-1 text-xs font-semibold text-warn',
            )}
          >
            <WifiOff size={13} /> 事件流{CONNECTION_LABEL[connection]}
          </span>
        )}
        <IconButton
          label={inspectorOpen ? '收起检视器 (Ctrl+J)' : '展开检视器 (Ctrl+J)'}
          aria-pressed={inspectorOpen}
          active={inspectorOpen}
          onClick={toggleInspector}
        >
          <PanelRight size={17} />
        </IconButton>
      </div>
    </header>
  );
}
