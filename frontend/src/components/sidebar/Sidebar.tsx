/**
 * 侧栏：工作区 → 会话两级树。没有 Logo；顶部一个"新建工作区"，底部主题切换与设置。
 * 图标只用线条 SVG。设计见 docs/10_frontend/03_visual_design.md §4.1。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'motion/react';
import {
  Copy,
  CopyPlus,
  FolderPlus,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Settings,
  Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import { TERMINAL_STATUSES, type Session, type Workspace } from '../../types/api';
import { cn } from '../../utils/text';
import { FolderGlyph } from '../icons/Glyphs';
import { ThemeToggle } from '../layout/ThemeToggle';
import { ConfirmDialog } from '../modals/ConfirmDialog';
import { RenameWorkspaceModal } from '../modals/RenameWorkspaceModal';
import { WorkspaceModal } from '../modals/WorkspaceModal';
import { SettingsModal } from '../settings/SettingsModal';
import { MoreMenu } from '../ui/MoreMenu';
import { Chevron, IconButton, Reveal, SPRING } from '../ui/primitives';

export function Sidebar() {
  const client = useQueryClient();
  const workspaceId = useAppStore((s) => s.workspaceId);
  const sessionId = useAppStore((s) => s.sessionId);
  const run = useAppStore((s) => s.run);
  const { selectWorkspace, selectSession } = useAppStore.getState();
  const collapsed = useUiStore((s) => s.sidebarCollapsed);
  const toggle = useUiStore((s) => s.toggleSidebar);
  const [adding, setAdding] = useState(false);
  const [settings, setSettings] = useState(false);
  const [renaming, setRenaming] = useState<Workspace | null>(null);
  const [removingWorkspace, setRemovingWorkspace] = useState<Workspace | null>(null);
  const [removingSession, setRemovingSession] = useState<{
    session: Session;
    label: string;
  } | null>(null);

  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.workspaces.list });
  const sessions = useQuery({
    queryKey: ['sessions', workspaceId],
    queryFn: () => api.sessions.list(workspaceId as string),
    enabled: !!workspaceId,
  });

  const newSession = useMutation({
    mutationFn: (id: string) => api.sessions.create(id),
    onSuccess: async (s) => {
      if (s.workspace_id !== useAppStore.getState().workspaceId) selectWorkspace(s.workspace_id);
      await client.invalidateQueries({ queryKey: ['sessions', s.workspace_id] });
      selectSession(s.session_id);
    },
  });

  const forkSession = useMutation({
    mutationFn: (id: string) => api.sessions.fork(id),
    onSuccess: async (s) => {
      await client.invalidateQueries({ queryKey: ['sessions', s.workspace_id] });
      selectSession(s.session_id);
      useAppStore.getState().notify('已创建副本');
    },
    onError: (e) => useAppStore.getState().notify((e as Error).message, 'error'),
  });

  const copyPath = (path: string) => {
    const done = () => useAppStore.getState().notify('路径已复制');
    const failed = () => useAppStore.getState().notify('复制失败，浏览器不允许访问剪贴板', 'error');
    if (!navigator.clipboard) return failed();
    navigator.clipboard.writeText(path).then(done, failed);
  };

  const removeSession = async (s: Session) => {
    await api.sessions.remove(s.session_id);
    if (useAppStore.getState().sessionId === s.session_id) selectSession(null);
    await client.invalidateQueries({ queryKey: ['sessions', s.workspace_id] });
  };

  const removeWorkspace = async (w: Workspace) => {
    await api.workspaces.remove(w.workspace_id);
    if (useAppStore.getState().workspaceId === w.workspace_id) selectWorkspace(null);
    await client.invalidateQueries({ queryKey: ['workspaces'] });
  };

  const live = !!run && !TERMINAL_STATUSES.includes(run.status);
  const waiting = run?.status === 'waiting_for_approval';
  const PanelIcon = collapsed ? PanelLeftOpen : PanelLeftClose;

  return (
    <motion.aside
      aria-label="侧栏"
      initial={false}
      animate={{ width: collapsed ? 64 : 260 }}
      transition={SPRING}
      className="relative z-20 flex h-full shrink-0 flex-col overflow-hidden bg-side/75 backdrop-blur-2xl backdrop-saturate-150"
    >
      <h1 className="sr-only">BaseAgent</h1>

      {/* 顶部：标题 + 新建工作区 */}
      <div
        className={cn(
          'flex h-14 shrink-0 items-center',
          collapsed ? 'justify-center' : 'gap-1 pl-5 pr-3',
        )}
      >
        {!collapsed && <span className="flex-1 text-[13px] font-semibold text-mute">工作区</span>}
        <IconButton label="添加工作区" onClick={() => setAdding(true)}>
          <FolderPlus size={17} strokeWidth={1.7} />
        </IconButton>
      </div>

      <nav
        aria-label="工作区与会话"
        className={cn(
          'min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-3 no-scrollbar',
          collapsed ? 'flex flex-col items-center gap-1 px-2' : 'px-2.5',
        )}
      >
        {workspaces.data && !workspaces.data.length && !collapsed && (
          <motion.p
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            className="mx-1.5 mt-1 rounded-2xl bg-ink/[0.035] px-3 py-4 text-center text-xs leading-relaxed text-mute"
          >
            还没有工作区，点右上角的文件夹按钮添加
          </motion.p>
        )}
        {workspaces.isLoading &&
          !collapsed &&
          [0, 1, 2].map((i) => (
            <div
              key={i}
              className="mx-2.5 my-2.5 h-3.5 animate-pulse rounded-full bg-ink/[0.06]"
              style={{ width: `${70 - i * 12}%` }}
            />
          ))}

        {workspaces.data?.map((w) => {
          const open = w.workspace_id === workspaceId;
          if (collapsed) {
            return (
              <button
                key={w.workspace_id}
                title={`${w.name}\n${w.root_path}`}
                aria-label={w.name}
                aria-current={open}
                className={cn(
                  'flex h-9 w-9 items-center justify-center rounded-xl transition-colors hover:bg-ink/[0.06]',
                  open && 'bg-ink/[0.07]',
                )}
                onClick={() => !open && selectWorkspace(w.workspace_id)}
              >
                <FolderGlyph open={open} size={17} />
              </button>
            );
          }
          const list = open ? sessions.data : undefined;
          return (
            <div key={w.workspace_id} className="mb-0.5">
              <div className="group relative">
                <button
                  aria-current={open}
                  title={w.root_path}
                  className="nav-item pr-[3.75rem]"
                  onClick={() => !open && selectWorkspace(w.workspace_id)}
                >
                  <Chevron open={open} size={12} />
                  <FolderGlyph open={open} size={16} />
                  <span className="min-w-0 flex-1 truncate">{w.name}</span>
                </button>
                <IconButton
                  label={`在 ${w.name} 中新建会话`}
                  disabled={newSession.isPending}
                  className={cn(
                    'absolute right-8 top-1/2 h-6 w-6 -translate-y-1/2 transition-opacity',
                    open
                      ? 'opacity-100'
                      : 'opacity-0 focus-visible:opacity-100 group-hover:opacity-100',
                  )}
                  onClick={() => newSession.mutate(w.workspace_id)}
                >
                  <Plus size={15} />
                </IconButton>
                <MoreMenu
                  label={`${w.name} 的更多操作`}
                  className="absolute right-1 top-1/2 h-6 w-6 -translate-y-1/2 opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100 aria-expanded:opacity-100"
                  items={[
                    {
                      label: '新建会话',
                      icon: <Plus size={15} />,
                      onSelect: () => newSession.mutate(w.workspace_id),
                    },
                    {
                      label: '重命名',
                      icon: <Pencil size={15} />,
                      onSelect: () => setRenaming(w),
                    },
                    {
                      label: '复制路径',
                      icon: <Copy size={15} />,
                      onSelect: () => copyPath(w.root_path),
                    },
                    {
                      label: '移除工作区',
                      icon: <Trash2 size={15} />,
                      danger: true,
                      separated: true,
                      onSelect: () => setRemovingWorkspace(w),
                    },
                  ]}
                />
              </div>
              <Reveal open={open}>
                <ul className="relative ml-[1.15rem] space-y-0.5 py-0.5 pl-2">
                  {list?.map((s, index) => {
                    const active = s.session_id === sessionId;
                    const label = `会话 ${list.length - index}`;
                    return (
                      <li key={s.session_id} className="group/session relative">
                        {active && (
                          <motion.span
                            layoutId="session-indicator"
                            transition={SPRING}
                            className="absolute -left-2 bottom-2 top-2 w-[3px] rounded-full bg-brand"
                          />
                        )}
                        <button
                          aria-current={active}
                          className="nav-item py-1.5 pr-8"
                          onClick={() => selectSession(s.session_id)}
                        >
                          <MessageSquare size={14} strokeWidth={1.6} className="shrink-0" />
                          <span className="min-w-0 flex-1 truncate">
                            {label}
                            <span className="ml-1.5 text-2xs font-normal tabular-nums text-mute/70">
                              {new Date(s.created_at * 1000).toLocaleDateString()}
                            </span>
                          </span>
                          {active && live && (
                            <span
                              aria-label={waiting ? '等待审批' : '运行中'}
                              className={cn(
                                'h-2 w-2 shrink-0 rounded-full',
                                waiting ? 'animate-halo bg-warn' : 'animate-pulse bg-brand',
                              )}
                            />
                          )}
                        </button>
                        <MoreMenu
                          label={`${label} 的更多操作`}
                          className="absolute right-1 top-1/2 h-6 w-6 -translate-y-1/2 opacity-0 transition-opacity focus-visible:opacity-100 group-hover/session:opacity-100 aria-expanded:opacity-100"
                          items={[
                            {
                              label: '创建副本',
                              icon: <CopyPlus size={15} />,
                              disabled: forkSession.isPending,
                              onSelect: () => forkSession.mutate(s.session_id),
                            },
                            {
                              label: '删除会话',
                              icon: <Trash2 size={15} />,
                              danger: true,
                              separated: true,
                              onSelect: () => setRemovingSession({ session: s, label }),
                            },
                          ]}
                        />
                      </li>
                    );
                  })}
                  {list && !list.length && (
                    <li className="px-2.5 py-1.5 text-xs text-mute">还没有会话，点 + 新建</li>
                  )}
                </ul>
              </Reveal>
            </div>
          );
        })}

        {collapsed && (
          <IconButton
            label="新建会话"
            disabled={!workspaceId || newSession.isPending}
            className="mt-1 disabled:opacity-40"
            onClick={() => workspaceId && newSession.mutate(workspaceId)}
          >
            <Plus size={17} />
          </IconButton>
        )}
      </nav>

      {/* 底部：主题与设置 */}
      <div className={cn('flex shrink-0 items-center gap-1 pb-3', collapsed ? 'flex-col' : 'px-3')}>
        <ThemeToggle />
        <IconButton label="设置" onClick={() => setSettings(true)}>
          <Settings size={17} strokeWidth={1.7} />
        </IconButton>
        <IconButton
          label={collapsed ? '展开侧栏 (Ctrl+B)' : '收起侧栏 (Ctrl+B)'}
          className={collapsed ? undefined : 'ml-auto'}
          onClick={toggle}
        >
          <PanelIcon size={17} strokeWidth={1.7} />
        </IconButton>
      </div>

      {adding && <WorkspaceModal onClose={() => setAdding(false)} />}
      {renaming && <RenameWorkspaceModal workspace={renaming} onClose={() => setRenaming(null)} />}
      {removingWorkspace && (
        <ConfirmDialog
          title="移除工作区"
          confirmLabel="移除"
          onConfirm={() => removeWorkspace(removingWorkspace)}
          onClose={() => setRemovingWorkspace(null)}
        >
          将移除「{removingWorkspace.name}」，并删除它下面的所有会话、对话记录和记忆。
          磁盘上的项目文件不会被删除。此操作无法撤销。
        </ConfirmDialog>
      )}
      {removingSession && (
        <ConfirmDialog
          title="删除会话"
          confirmLabel="删除"
          onConfirm={() => removeSession(removingSession.session)}
          onClose={() => setRemovingSession(null)}
        >
          将删除「{removingSession.label}」的对话记录、记忆和任务记录。此操作无法撤销。
        </ConfirmDialog>
      )}
      {settings && <SettingsModal onClose={() => setSettings(false)} />}
    </motion.aside>
  );
}
