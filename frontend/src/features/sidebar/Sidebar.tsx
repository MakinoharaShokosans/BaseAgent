/**
 * 侧栏：需要处理的会话、工作区 → 会话树、搜索、底部入口。
 * 手机宽度下由外壳放进抽屉；桌面可折叠成图标条。
 */
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import {
  Archive,
  ArchiveRestore,
  ChevronRight,
  FolderPlus,
  GitFork,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  Trash2,
  X,
} from 'lucide-react';
import { motion } from 'motion/react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/endpoints';
import {
  hasSessionMeta,
  isLive,
  sessionTitle,
  useSessionActivity,
  useWorkspaces,
  type Activity,
} from '../../api/queries';
import { ThemeToggle } from '../../components/layout/ThemeToggle';
import { ConfirmDialog } from '../../components/modals/ConfirmDialog';
import { RenameWorkspaceModal } from '../../components/modals/RenameWorkspaceModal';
import { WorkspaceModal } from '../../components/modals/WorkspaceModal';
import { InlineEdit } from '../../components/ui/InlineEdit';
import { MoreMenu } from '../../components/ui/MoreMenu';
import { StatusDot } from '../../components/ui/StatusDot';
import { IconButton, Reveal } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import type { Session, Workspace } from '../../types/api';
import { cn } from '../../utils/text';
import { relativeTime } from '../../utils/time';

const PER_WORKSPACE_ACTIVITY = 12;

/** 所有工作区的会话列表，按工作区分组 */
function useAllSessions(workspaces: Workspace[]) {
  const results = useQueries({
    queries: workspaces.map((w) => ({
      queryKey: ['sessions', w.workspace_id],
      queryFn: () => api.sessions.list(w.workspace_id),
    })),
  });
  const map: Record<string, Session[]> = {};
  workspaces.forEach((w, i) => {
    map[w.workspace_id] = results[i]?.data ?? [];
  });
  return map;
}

function sortSessions(list: Session[]): Session[] {
  return [...list].sort(
    (a, b) =>
      Number(!!b.pinned) - Number(!!a.pinned) ||
      (b.updated_at ?? b.created_at) - (a.updated_at ?? a.created_at),
  );
}

export function Sidebar({ collapsedRail = false }: { collapsedRail?: boolean }) {
  const workspaces = useWorkspaces();
  const list = useMemo(() => workspaces.data ?? [], [workspaces.data]);
  const sessionsBy = useAllSessions(list);
  const params = useParams({ strict: false }) as { workspaceId?: string; sessionId?: string };
  const navigate = useNavigate();
  const client = useQueryClient();
  const phone = useUiStore((s) => s.phone);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const openWorkspaces = useUiStore((s) => s.openWorkspaces);
  const workspacesTouched = useUiStore((s) => s.workspacesTouched);
  const setWorkspaceOpen = useUiStore((s) => s.setWorkspaceOpen);
  const notify = useAppStore((s) => s.notify);

  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState(false);
  const [renamingWs, setRenamingWs] = useState<Workspace | null>(null);
  const [deletingWs, setDeletingWs] = useState<Workspace | null>(null);
  const [deletingSession, setDeletingSession] = useState<Session | null>(null);
  const [editingSession, setEditingSession] = useState<string | null>(null);

  // 活动状态：每个工作区最近的若干会话（未归档）
  const watched = useMemo(
    () =>
      list.flatMap((w) =>
        sortSessions(sessionsBy[w.workspace_id] ?? [])
          .filter((s) => !s.archived)
          .slice(0, PER_WORKSPACE_ACTIVITY)
          .map((s) => s.session_id),
      ),
    [list, sessionsBy],
  );
  const activity = useSessionActivity(watched);

  const attention = useMemo(() => {
    const out: { session: Session; workspace: Workspace; activity: Activity }[] = [];
    for (const w of list)
      for (const s of sessionsBy[w.workspace_id] ?? []) {
        const a = activity[s.session_id];
        if (a && (a.status === 'waiting_for_approval' || a.status === 'failed') && !s.archived)
          out.push({ session: s, workspace: w, activity: a });
      }
    return out
      .filter(
        (x) =>
          x.session.session_id !== params.sessionId || x.activity.status === 'waiting_for_approval',
      )
      .sort(
        (a, b) =>
          Number(b.activity.status === 'waiting_for_approval') -
          Number(a.activity.status === 'waiting_for_approval'),
      );
  }, [list, sessionsBy, activity, params.sessionId]);

  const refresh = (workspaceId: string) =>
    client.invalidateQueries({ queryKey: ['sessions', workspaceId] });

  const go = (workspaceId: string, sessionId?: string) => {
    if (phone) setNavOpen(false);
    void (sessionId
      ? navigate({ to: '/w/$workspaceId/s/$sessionId', params: { workspaceId, sessionId } })
      : navigate({ to: '/w/$workspaceId', params: { workspaceId } }));
  };

  const create = useMutation({
    mutationFn: (workspaceId: string) => api.sessions.create(workspaceId),
    onSuccess: async (session) => {
      await refresh(session.workspace_id);
      setWorkspaceOpen(session.workspace_id, true);
      go(session.workspace_id, session.session_id);
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const patch = useMutation({
    mutationFn: ({
      session,
      body,
    }: {
      session: Session;
      body: Parameters<typeof api.sessions.update>[1];
    }) => api.sessions.update(session.session_id, body),
    onSuccess: (_d, v) => refresh(v.session.workspace_id),
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const fork = useMutation({
    mutationFn: (session: Session) => api.sessions.fork(session.session_id),
    onSuccess: async (next) => {
      await refresh(next.workspace_id);
      go(next.workspace_id, next.session_id);
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const currentWorkspaceId = params.workspaceId ?? list[0]?.workspace_id;
  const q = query.trim().toLowerCase();

  // 首次进入时自动展开当前工作区；用户手动开合过之后尊重保存的状态
  const opened = useRef<string | null>(null);
  useEffect(() => {
    if (workspacesTouched || !currentWorkspaceId || opened.current === currentWorkspaceId) return;
    opened.current = currentWorkspaceId;
    if (!openWorkspaces.includes(currentWorkspaceId)) setWorkspaceOpen(currentWorkspaceId, true);
    // 只在进入时判断一次；openWorkspaces 的变化不再触发自动展开
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId, workspacesTouched, setWorkspaceOpen]);

  if (collapsedRail) {
    return (
      <aside
        aria-label="侧栏"
        className="relative z-drawer flex h-full w-[56px] shrink-0 flex-col items-center gap-1 border-r border-ink/[0.06] bg-side/80 py-3 backdrop-blur-2xl dark:border-white/[0.06]"
      >
        <IconButton label="展开侧栏" hint="Ctrl+B" side="right" onClick={toggleSidebar}>
          <PanelLeftOpen size={17} />
        </IconButton>
        <IconButton
          label="新会话"
          side="right"
          disabled={!currentWorkspaceId}
          onClick={() => currentWorkspaceId && create.mutate(currentWorkspaceId)}
        >
          <Plus size={17} />
        </IconButton>
        <div className="relative">
          <IconButton
            label={attention.length ? `${attention.length} 个会话需要处理` : '没有待处理'}
            side="right"
            onClick={() =>
              attention[0] &&
              go(attention[0].workspace.workspace_id, attention[0].session.session_id)
            }
          >
            <StatusDot status={attention[0]?.activity.status ?? null} size={10} />
          </IconButton>
        </div>
        <div className="mt-1 flex min-h-0 flex-1 flex-col items-center gap-1 overflow-y-auto">
          {list.map((w) => (
            <IconButton
              key={w.workspace_id}
              label={w.name}
              side="right"
              active={w.workspace_id === params.workspaceId}
              onClick={() => go(w.workspace_id)}
            >
              <span className="text-[12px] font-semibold">{w.name.slice(0, 1).toUpperCase()}</span>
            </IconButton>
          ))}
        </div>
        <ThemeToggle />
        <Link to="/settings">
          <IconButton label="设置" side="right" tabIndex={-1}>
            <Settings size={17} />
          </IconButton>
        </Link>
      </aside>
    );
  }

  return (
    <aside
      aria-label="侧栏"
      className="relative z-drawer flex h-full w-[280px] max-w-[86vw] shrink-0 flex-col overflow-hidden border-r border-ink/[0.06] bg-side/80 backdrop-blur-2xl backdrop-saturate-150 dark:border-white/[0.06]"
    >
      <div className="flex h-12 shrink-0 items-center gap-1 px-3">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand text-[13px] font-bold text-white">
          B
        </span>
        <span className="min-w-0 flex-1" />
        <IconButton
          label={searching ? '关闭搜索' : '搜索会话'}
          active={searching}
          onClick={() => {
            setSearching((v) => !v);
            setQuery('');
          }}
        >
          <Search size={16} />
        </IconButton>
        <IconButton label="添加工作区" onClick={() => setAdding(true)}>
          <FolderPlus size={16} />
        </IconButton>
        {phone ? (
          <IconButton label="关闭" onClick={() => setNavOpen(false)}>
            <X size={16} />
          </IconButton>
        ) : (
          <IconButton label="折叠侧栏" hint="Ctrl+B" onClick={toggleSidebar}>
            <PanelLeftClose size={16} />
          </IconButton>
        )}
      </div>

      <Reveal open={searching}>
        <div className="px-3 pb-2">
          <input
            autoFocus
            aria-label="搜索会话"
            className="field py-1.5 text-[13px]"
            placeholder="标题"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </Reveal>

      <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
        {!q && attention.length > 0 && (
          <section aria-label="需要处理" className="mb-2 rounded-2xl bg-warn/[0.07] p-1.5">
            {attention.map(({ session, workspace, activity: a }) => (
              <button
                key={session.session_id}
                onClick={() => go(workspace.workspace_id, session.session_id)}
                className="flex w-full items-center gap-2 rounded-xl px-2 py-1.5 text-left text-[13px] hover:bg-ink/[0.05]"
              >
                <StatusDot status={a.status} />
                <span className="min-w-0 flex-1 truncate">{sessionTitle(session, a)}</span>
                <span className="shrink-0 text-2xs text-mute">{workspace.name}</span>
              </button>
            ))}
          </section>
        )}

        {list.map((w) => {
          const all = sortSessions(sessionsBy[w.workspace_id] ?? []);
          const visible = all.filter((s) => !s.archived);
          const archived = all.filter((s) => s.archived);
          const filtered = q
            ? all.filter((s) => sessionTitle(s, activity[s.session_id]).toLowerCase().includes(q))
            : visible;
          if (q && filtered.length === 0) return null;
          const open = q ? true : openWorkspaces.includes(w.workspace_id);
          const busy = visible.some((s) => isLive(activity[s.session_id]?.status));
          return (
            <WorkspaceNode
              key={w.workspace_id}
              workspace={w}
              open={open}
              busy={busy}
              current={w.workspace_id === params.workspaceId}
              onToggle={() => setWorkspaceOpen(w.workspace_id, !open)}
              onOpenPage={() => go(w.workspace_id)}
              onNewSession={() => create.mutate(w.workspace_id)}
              onRename={() => setRenamingWs(w)}
              onDelete={() => setDeletingWs(w)}
            >
              {filtered.map((s) => (
                <SessionRow
                  key={s.session_id}
                  session={s}
                  activity={activity[s.session_id]}
                  current={s.session_id === params.sessionId}
                  editing={editingSession === s.session_id}
                  onOpen={() => go(w.workspace_id, s.session_id)}
                  onRename={(title) => {
                    setEditingSession(null);
                    patch.mutate({ session: s, body: { title } });
                  }}
                  onStartRename={() => setEditingSession(s.session_id)}
                  onCancelRename={() => setEditingSession(null)}
                  onPin={() => patch.mutate({ session: s, body: { pinned: !s.pinned } })}
                  onArchive={() => patch.mutate({ session: s, body: { archived: !s.archived } })}
                  onFork={() => fork.mutate(s)}
                  onDelete={() => setDeletingSession(s)}
                />
              ))}
              {!q && archived.length > 0 && (
                <ArchivedGroup count={archived.length}>
                  {archived.map((s) => (
                    <SessionRow
                      key={s.session_id}
                      session={s}
                      activity={activity[s.session_id]}
                      current={s.session_id === params.sessionId}
                      editing={false}
                      onOpen={() => go(w.workspace_id, s.session_id)}
                      onRename={() => undefined}
                      onStartRename={() => undefined}
                      onCancelRename={() => undefined}
                      onPin={() => undefined}
                      onArchive={() => patch.mutate({ session: s, body: { archived: false } })}
                      onFork={() => fork.mutate(s)}
                      onDelete={() => setDeletingSession(s)}
                    />
                  ))}
                </ArchivedGroup>
              )}
              {!q && visible.length === 0 && archived.length === 0 && (
                <button
                  onClick={() => create.mutate(w.workspace_id)}
                  className="flex w-full items-center gap-2 rounded-xl px-2.5 py-1.5 text-[13px] text-mute hover:bg-ink/[0.05] hover:text-ink"
                >
                  <Plus size={14} /> 新会话
                </button>
              )}
            </WorkspaceNode>
          );
        })}

        {workspaces.isSuccess && list.length === 0 && (
          <button
            onClick={() => setAdding(true)}
            className="mt-6 flex w-full flex-col items-center gap-2 rounded-2xl border border-dashed border-ink/[0.15] py-8 text-mute hover:border-accent/50 hover:text-ink"
          >
            <FolderPlus size={22} />
          </button>
        )}
      </nav>

      <div className="flex h-12 shrink-0 items-center gap-1 border-t border-ink/[0.06] px-3 dark:border-white/[0.06]">
        <Link to="/settings" onClick={() => phone && setNavOpen(false)}>
          <IconButton label="设置" tabIndex={-1}>
            <Settings size={17} />
          </IconButton>
        </Link>
        <ThemeToggle />
      </div>

      {adding && <WorkspaceModal onClose={() => setAdding(false)} />}
      {renamingWs && (
        <RenameWorkspaceModal workspace={renamingWs} onClose={() => setRenamingWs(null)} />
      )}
      {deletingWs && (
        <ConfirmDialog
          title="删除工作区"
          confirmLabel="删除"
          requireText={deletingWs.name}
          onClose={() => setDeletingWs(null)}
          onConfirm={async () => {
            await api.workspaces.remove(deletingWs.workspace_id);
            await client.invalidateQueries({ queryKey: ['workspaces'] });
            if (params.workspaceId === deletingWs.workspace_id) void navigate({ to: '/' });
          }}
        >
          将删除「{deletingWs.name}」下的
          <strong className="text-ink">
            {' '}
            {sessionsBy[deletingWs.workspace_id]?.length ?? 0} 个会话
          </strong>
          、全部记忆与任务记录。磁盘上的项目文件不受影响。
        </ConfirmDialog>
      )}
      {deletingSession && (
        <ConfirmDialog
          title="删除会话"
          confirmLabel="删除"
          onClose={() => setDeletingSession(null)}
          onConfirm={async () => {
            await api.sessions.remove(deletingSession.session_id);
            await refresh(deletingSession.workspace_id);
            if (params.sessionId === deletingSession.session_id) go(deletingSession.workspace_id);
          }}
        >
          会话及其对话、记忆将被删除。
        </ConfirmDialog>
      )}
    </aside>
  );
}

function WorkspaceNode({
  workspace,
  open,
  busy,
  current,
  onToggle,
  onOpenPage,
  onNewSession,
  onRename,
  onDelete,
  children,
}: {
  workspace: Workspace;
  open: boolean;
  busy: boolean;
  current: boolean;
  onToggle: () => void;
  onOpenPage: () => void;
  onNewSession: () => void;
  onRename: () => void;
  onDelete: () => void;
  children: React.ReactNode;
}) {
  return (
    <section aria-label={workspace.name}>
      <div
        className={cn(
          'group flex h-9 items-center gap-1 rounded-xl pl-1 pr-0.5 transition-colors hover:bg-ink/[0.045]',
          current && 'bg-ink/[0.04]',
        )}
      >
        <button
          aria-expanded={open}
          aria-label={open ? '折叠' : '展开'}
          onClick={onToggle}
          className="flex h-7 w-6 shrink-0 items-center justify-center text-mute"
        >
          <ChevronRight
            size={14}
            className={cn('transition-transform duration-200', open && 'rotate-90')}
          />
        </button>
        <button
          onClick={onOpenPage}
          className="flex min-w-0 flex-1 items-center gap-2 text-left text-[13.5px] font-semibold"
        >
          <span className="truncate">{workspace.name}</span>
          {busy && <StatusDot status="running" size={6} />}
        </button>
        <div className="flex shrink-0 items-center opacity-100 sm:opacity-0 sm:transition-opacity sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
          <IconButton label="新会话" className="h-7 w-7" onClick={onNewSession}>
            <Plus size={15} />
          </IconButton>
          <MoreMenu
            label="工作区操作"
            className="h-7 w-7"
            items={[
              { label: '概览与设置', icon: <LayoutDashboard size={14} />, onSelect: onOpenPage },
              { label: '重命名', icon: <Pencil size={14} />, onSelect: onRename },
              {
                label: '删除',
                icon: <Trash2 size={14} />,
                danger: true,
                separated: true,
                onSelect: onDelete,
              },
            ]}
          />
        </div>
      </div>
      <Reveal open={open}>
        <div className="ml-3 space-y-0.5 border-l border-ink/[0.07] py-0.5 pl-1.5 dark:border-white/[0.07]">
          {children}
        </div>
      </Reveal>
    </section>
  );
}

function ArchivedGroup({ count, children }: { count: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-2xs text-mute hover:text-ink"
      >
        <Archive size={12} /> {count}
        <ChevronRight size={11} className={cn('transition-transform', open && 'rotate-90')} />
      </button>
      <Reveal open={open}>
        <div className="opacity-70">{children}</div>
      </Reveal>
    </div>
  );
}

function SessionRow({
  session,
  activity,
  current,
  editing,
  onOpen,
  onRename,
  onStartRename,
  onCancelRename,
  onPin,
  onArchive,
  onFork,
  onDelete,
}: {
  session: Session;
  activity: Activity | undefined;
  current: boolean;
  editing: boolean;
  onOpen: () => void;
  onRename: (title: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onPin: () => void;
  onArchive: () => void;
  onFork: () => void;
  onDelete: () => void;
}) {
  const title = sessionTitle(session, activity);
  const when = session.updated_at ?? (activity?.status ? activity.at : session.created_at);
  const manageable = hasSessionMeta(session);
  return (
    <motion.div layout="position" transition={{ duration: 0.18 }} className="group relative">
      <div
        role="link"
        tabIndex={0}
        aria-current={current ? 'page' : undefined}
        onClick={editing ? undefined : onOpen}
        onKeyDown={(e) => {
          if (!editing && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            onOpen();
          }
        }}
        onDoubleClick={manageable ? onStartRename : undefined}
        className={cn(
          'flex h-8 cursor-pointer items-center gap-2 rounded-lg pl-2 pr-1 text-[13px] text-mute transition-colors hover:bg-ink/[0.05] hover:text-ink',
          current && 'bg-ink/[0.07] font-medium text-ink',
        )}
      >
        <StatusDot status={activity?.status} size={7} />
        {editing ? (
          <InlineEdit
            label="会话标题"
            value={title}
            onCommit={onRename}
            onCancel={onCancelRename}
          />
        ) : (
          <>
            <span className="min-w-0 flex-1 truncate">{title}</span>
            {session.pinned && (
              <Pin size={11} className="shrink-0 text-accent" aria-label="已置顶" />
            )}
            <span className="hidden shrink-0 text-2xs tabular-nums text-mute/70 sm:inline sm:group-focus-within:hidden sm:group-hover:hidden">
              {relativeTime(when)}
            </span>
          </>
        )}
        {!editing && (
          // 手机上常驻；桌面上悬停或聚焦时才出现，避免挤占标题
          <span
            className="flex shrink-0 opacity-100 sm:pointer-events-none sm:opacity-0 sm:group-focus-within:pointer-events-auto sm:group-focus-within:opacity-100 sm:group-hover:pointer-events-auto sm:group-hover:opacity-100"
            onClick={(e) => e.stopPropagation()}
          >
            <MoreMenu
              label="会话操作"
              className="h-6 w-6"
              items={[
                {
                  label: '重命名',
                  icon: <Pencil size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onStartRename,
                },
                {
                  label: session.pinned ? '取消置顶' : '置顶',
                  icon: session.pinned ? <PinOff size={14} /> : <Pin size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onPin,
                },
                {
                  label: session.archived ? '取消归档' : '归档',
                  icon: session.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onArchive,
                },
                { label: '分支', icon: <GitFork size={14} />, onSelect: onFork },
                {
                  label: '删除',
                  icon: <Trash2 size={14} />,
                  danger: true,
                  separated: true,
                  onSelect: onDelete,
                },
              ]}
            />
          </span>
        )}
      </div>
    </motion.div>
  );
}
