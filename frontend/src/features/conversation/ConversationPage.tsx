/** 对话页：消息流 + 输入台 + 右侧检视器。 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowDown, ChevronRight, Menu, MessageSquare, PanelRight, WifiOff } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/endpoints';
import {
  hasSessionMeta,
  sessionTitle,
  useSessionTasks,
  useSessions,
  useWorkspaces,
} from '../../api/queries';
import { useContextSnapshot, useSessionSettings } from '../../api/useSettings';
import { SafeMarkdown } from '../../components/common/SafeMarkdown';
import { McpStatus } from '../../components/domain/McpStatus';
import { InlineEdit } from '../../components/ui/InlineEdit';
import { IconButton, SPRING } from '../../components/ui/primitives';
import { Tip } from '../../components/ui/Tooltip';
import { useAppStore } from '../../stores/useAppStore';
import { queueOf, useQueueStore } from '../../stores/useQueueStore';
import { useUiStore } from '../../stores/useUiStore';
import { TERMINAL_STATUSES, type PermissionLevel, type Task, type Turn } from '../../types/api';
import { rememberLocation } from '../../utils/lastLocation';
import {
  CONNECTION_LABEL,
  MinimalStatusMark,
  connectionHealthy,
  statusMeta,
} from '../../utils/status';
import { cn } from '../../utils/text';
import { Inspector } from '../inspector/Inspector';
import { Composer } from './Composer';
import { ConversationTimelineRail, type DialogueNode } from './ConversationTimelineRail';
import { RunView } from './RunView';
import { useReplay } from './useReplay';

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-ink/[0.06] px-3.5 py-2 text-[14.5px] leading-relaxed">
        {text}
      </div>
    </div>
  );
}

function PastTask({ task, turn }: { task: Task; turn: Turn | undefined }) {
  const [open, setOpen] = useState(false);
  const replay = useReplay(task.task_id, task.task_goal, open);
  const meta = statusMeta(task.status);
  return (
    <div className="space-y-2">
      {open && replay ? (
        <RunView
          run={{ ...replay, delivery: replay.delivery || turn?.content || '' }}
          live={false}
        />
      ) : (
        turn?.content && (
          <div className="py-1">
            <SafeMarkdown text={turn.content} />
          </div>
        )
      )}
      <div className="flex items-center gap-1.5 border-t border-ink/[0.06] pt-2 text-[12px] text-mute dark:border-white/[0.06]">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-1.5 transition-colors hover:text-ink"
        >
          <ChevronRight size={12} className={cn('transition-transform', open && 'rotate-90')} />
          <span className="underline decoration-ink/15 decoration-dashed underline-offset-2 hover:decoration-ink/40 dark:decoration-white/20 dark:hover:decoration-white/50">
            {open ? '收起过程' : '过程'}
          </span>
          {task.status !== 'succeeded' && <span className="text-bad">{meta.label}</span>}
          <span className="tabular-nums">{task.step_count} 回合</span>
        </button>
      </div>
    </div>
  );
}

export function ConversationPage() {
  const { workspaceId = '', sessionId = '' } = useParams({ strict: false }) as {
    workspaceId?: string;
    sessionId?: string;
  };
  const client = useQueryClient();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { task?: string };
  const workspaces = useWorkspaces();
  const sessions = useSessions(workspaceId);
  const tasks = useSessionTasks(sessionId, 50);
  const turns = useQuery({
    queryKey: ['turns', sessionId],
    queryFn: () => api.sessions.turns(sessionId, 200),
    enabled: !!sessionId,
  });
  const snapshot = useContextSnapshot(sessionId);
  const { query: settings } = useSessionSettings(sessionId);

  const run = useAppStore((s) => (s.runSession === sessionId ? s.run : null));
  const runSession = useAppStore((s) => s.runSession);
  const connection = useAppStore((s) => s.connection);
  const submit = useAppStore((s) => s.submit);
  const attach = useAppStore((s) => s.attach);
  const detach = useAppStore((s) => s.detach);
  const cancel = useAppStore((s) => s.cancel);
  const notify = useAppStore((s) => s.notify);

  const phone = useUiStore((s) => s.phone);
  const narrow = useUiStore((s) => s.narrow);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const inspectorOpen = useUiStore((s) => s.inspectorOpen);
  const drawerOpen = useUiStore((s) => s.drawerOpen);
  const toggleInspector = useUiStore((s) => s.toggleInspector);
  const markSessionRead = useUiStore((s) => s.markSessionRead);

  const queue = useQueueStore((s) => queueOf(s, sessionId));
  const session = sessions.data?.find((s) => s.session_id === sessionId);
  const workspace = workspaces.data?.find((w) => w.workspace_id === workspaceId);
  const ordered = useMemo(() => [...(tasks.data ?? [])].reverse(), [tasks.data]);
  const latest = tasks.data?.[0];
  const terminal = !!run && TERMINAL_STATUSES.includes(run.status);
  const busy = !!run && !terminal;
  const [renaming, setRenaming] = useState(false);

  // 切换会话：停掉旧订阅；会话变了就清掉旧任务视图
  useEffect(() => {
    if (useAppStore.getState().runSession !== sessionId) detach();
  }, [sessionId, detach]);

  // 记住位置，供根路径返回
  useEffect(() => {
    if (workspaceId && sessionId) rememberLocation(workspaceId, sessionId);
  }, [workspaceId, sessionId]);

  // ?task=:id：深链接定位到某个历史任务；只解析一次，之后不再覆盖用户的操作
  const deepLinked = useRef<string | null>(null);
  useEffect(() => {
    const wanted = search.task;
    if (!wanted || deepLinked.current === wanted) return;
    deepLinked.current = wanted;
    const local = tasks.data?.find((t) => t.task_id === wanted);
    if (local) {
      void attach(sessionId, local);
      return;
    }
    void api.tasks
      .get(wanted)
      .then((task) => {
        if (task.session_id !== sessionId) {
          notify('该任务不属于这个会话', 'error');
          return;
        }
        return attach(sessionId, task);
      })
      .catch((e: Error) => notify(e.message, 'error'));
  }, [search.task, sessionId, tasks.data, attach, notify]);

  // 打开会话时接上最近的任务（进行中的继续订阅，已结束的回放事件库）
  useEffect(() => {
    const waitingForLink = !!search.task && run?.taskId !== search.task;
    if (!latest || runSession === sessionId || waitingForLink) return;
    void attach(sessionId, latest);
  }, [latest, sessionId, runSession, search.task, run?.taskId, attach]);

  // 刷新后遗留的队列不自动继续：没有前端就没有调度者
  useEffect(() => {
    const q = queueOf(useQueueStore.getState(), sessionId);
    if (q.items.length && !q.paused) useQueueStore.getState().setPaused(sessionId, true);
  }, [sessionId]);

  // 任务结束：刷新依赖它的查询
  useEffect(() => {
    if (!terminal) return;
    for (const key of ['tasks', 'turns', 'activity', 'context', 'smemory', 'sessions'])
      void client.invalidateQueries({ queryKey: [key] });
  }, [terminal, client]);

  // 队列调度
  const submitting = useRef(false);
  const handled = useRef(new Set<string>());
  useEffect(() => {
    const q = queueOf(useQueueStore.getState(), sessionId);
    if (!q.items.length || submitting.current) return;
    const idle = !run || terminal;
    if (!idle) return;
    if (run && terminal && run.status !== 'succeeded') {
      if (!q.paused && !handled.current.has(run.taskId)) {
        handled.current.add(run.taskId);
        useQueueStore.getState().setPaused(sessionId, true);
      }
      return;
    }
    if (q.paused) return;
    const message = useQueueStore.getState().shift(sessionId);
    if (!message) return;
    submitting.current = true;
    void submit(sessionId, message.text, message.level, message.level === 'full_permissions')
      .then((task) => {
        if (!task) {
          useQueueStore.getState().unshift(sessionId, message);
          useQueueStore.getState().setPaused(sessionId, true);
        } else void client.invalidateQueries({ queryKey: ['tasks'] });
      })
      .finally(() => {
        submitting.current = false;
      });
  }, [run, terminal, queue, sessionId, submit, client]);

  // 首个任务成功后，由对话所选模型生成会话标题（只试一次）
  const titled = useRef(new Set<string>());
  useEffect(() => {
    if (
      run?.status !== 'succeeded' ||
      !session ||
      !hasSessionMeta(session) ||
      session.title ||
      titled.current.has(sessionId)
    )
      return;
    if ((tasks.data?.length ?? 0) !== 1) return;
    titled.current.add(sessionId);
    api.sessions
      .generateTitle(sessionId)
      .then(() => client.invalidateQueries({ queryKey: ['sessions', workspaceId] }))
      .catch(() => undefined);
  }, [run?.status, session, sessionId, tasks.data, workspaceId, client]);

  // 滚动：位于底部时跟随新内容，向上翻阅则停止并出现“回到最新”
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [away, setAway] = useState(false);
  // 滚动跟随：文本增量与命令实时输出都算“新内容”
  const tail = run?.items.at(-1);
  const tailSize =
    tail && 'text' in tail
      ? tail.text.length
      : tail && 'stdout' in tail
        ? tail.stdout.length + tail.stderr.length
        : 0;
  const tick = (run?.items.length ?? 0) + tailSize;
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) {
      el.scrollTop = el.scrollHeight;
      markSessionRead(sessionId);
    }
  }, [sessionId, tick, run?.status, ordered.length, markSessionRead]);
  useEffect(() => {
    stick.current = true;
    setAway(false);
    markSessionRead(sessionId);
  }, [sessionId, markSessionRead]);

  const turnByTask = useMemo(() => {
    const map = new Map<string, Turn>();
    for (const t of turns.data ?? []) if (t.role === 'assistant') map.set(t.task_id, t);
    return map;
  }, [turns.data]);

  const limit = settings.data
    ? (settings.data.context_token_limit ?? settings.data.defaults.context_token_limit)
    : snapshot.data?.context_window;
  const percent = snapshot.data && limit ? (snapshot.data.total_tokens / limit) * 100 : null;
  const meta = run ? statusMeta(run.status) : null;
  const inspectorShown = narrow ? drawerOpen : inspectorOpen;

  const title = session
    ? sessionTitle(session, { status: null, goal: latest?.task_goal ?? '', at: 0 })
    : '';
  const known = run && ordered.some((t) => t.task_id === run.taskId);

  const allTasks = useMemo(() => {
    const list = [...ordered];
    if (run && !known) {
      list.push({
        task_id: run.taskId,
        task_goal: run.goal,
        status: run.status,
        session_id: sessionId,
        workspace_id: workspaceId,
        created_at: run.startedTs ?? 0,
        updated_at: run.finishedTs ?? 0,
        step_count: run.stepCount,
      } as unknown as Task);
    }
    return list;
  }, [ordered, run, known, sessionId, workspaceId]);

  const timelineDialogues: DialogueNode[] = useMemo(() => {
    return allTasks.map((t, index) => ({
      id: t.task_id,
      goal: t.task_goal,
      index,
    }));
  }, [allTasks]);

  const [activeTaskIndex, setActiveTaskIndex] = useState(0);

  const scrollToTurn = useCallback(
    (index: number) => {
      const el = document.getElementById(`chat-turn-${index}`);
      if (el) {
        stick.current = index === timelineDialogues.length - 1;
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        setActiveTaskIndex(index);
      }
    },
    [timelineDialogues.length],
  );

  return (
    <div className="flex h-full min-h-0">
      <div className="relative flex min-w-0 flex-1 flex-col">
        <header className="relative z-sticky flex h-12 shrink-0 items-center gap-2 border-b border-ink/[0.06] bg-canvas/80 px-3 backdrop-blur-2xl dark:border-white/[0.06] sm:px-4">
          {phone && (
            <IconButton label="导航" onClick={() => setNavOpen(true)}>
              <Menu size={17} />
            </IconButton>
          )}
          <nav aria-label="位置" className="flex min-w-0 items-center gap-1.5 text-[13px]">
            {!phone && (
              <>
                <Tip label={workspace?.root_path ?? ''} side="bottom" align="start">
                  <button
                    className="max-w-[10rem] truncate font-semibold hover:text-accent"
                    onClick={() =>
                      void navigate({ to: '/w/$workspaceId', params: { workspaceId } })
                    }
                  >
                    {workspace?.name ?? ''}
                  </button>
                </Tip>
                <ChevronRight size={12} className="shrink-0 text-mute/50" />
              </>
            )}
            {renaming && session ? (
              <InlineEdit
                label="会话标题"
                value={title}
                onCancel={() => setRenaming(false)}
                onCommit={(next) => {
                  setRenaming(false);
                  api.sessions
                    .update(sessionId, { title: next })
                    .then(() => client.invalidateQueries({ queryKey: ['sessions', workspaceId] }))
                    .catch((e: Error) => notify(e.message, 'error'));
                }}
              />
            ) : (
              <button
                className={cn(
                  'min-w-0 truncate text-mute hover:text-ink',
                  phone && 'flex-1 text-left',
                )}
                onDoubleClick={
                  session && hasSessionMeta(session) ? () => setRenaming(true) : undefined
                }
              >
                {title}
              </button>
            )}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {run && meta && (
              <MinimalStatusMark status={run.status} meta={meta} className="px-1" />
            )}
            {!connectionHealthy(connection) && (
              <Tip label={`事件流${CONNECTION_LABEL[connection]}`}>
                <span
                  role="status"
                  className="flex h-7 w-7 items-center justify-center rounded-full bg-warn/12 text-warn"
                >
                  <WifiOff size={14} />
                </span>
              </Tip>
            )}
            <McpStatus />
            <IconButton
              label="检视器"
              hint="Ctrl+J"
              active={inspectorShown}
              onClick={toggleInspector}
            >
              <PanelRight size={17} />
            </IconButton>
          </div>
        </header>

        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={scroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              stick.current = nearBottom;
              setAway(!nearBottom);
              if (nearBottom) {
                markSessionRead(sessionId);
                if (timelineDialogues.length) setActiveTaskIndex(timelineDialogues.length - 1);
              } else if (el.scrollTop < 40) {
                setActiveTaskIndex(0);
              } else {
                const containerTop = el.getBoundingClientRect().top;
                let bestIndex = 0;
                let minDiff = Infinity;
                for (let i = 0; i < timelineDialogues.length; i++) {
                  const turnEl = document.getElementById(`chat-turn-${i}`);
                  if (!turnEl) continue;
                  const rect = turnEl.getBoundingClientRect();
                  const diff = Math.abs(rect.top - containerTop - 60);
                  if (diff < minDiff) {
                    minDiff = diff;
                    bestIndex = i;
                  }
                }
                setActiveTaskIndex(bestIndex);
              }
            }}
            className="no-scrollbar min-h-0 flex-1 overflow-y-auto"
          >
            <div className="mx-auto w-full max-w-[780px] space-y-5 px-3 py-5 sm:px-5">
              {allTasks.map((t, index) => (
                <div
                  key={t.task_id}
                  id={`chat-turn-${index}`}
                  data-task-index={index}
                  className="scroll-mt-4 space-y-3"
                >
                  <UserMessage text={t.task_goal} />
                  {run && run.taskId === t.task_id ? (
                    <RunView run={run} live />
                  ) : (
                    <PastTask task={t} turn={turnByTask.get(t.task_id)} />
                  )}
                </div>
              ))}
              {!allTasks.length && !run && tasks.isSuccess && (
                <div className="flex flex-col items-center gap-3 py-28 text-mute/35" aria-hidden>
                  <MessageSquare size={30} strokeWidth={1.4} />
                </div>
              )}
            </div>
          </div>

          <ConversationTimelineRail
            dialogues={timelineDialogues}
            activeIndex={activeTaskIndex}
            onSelect={scrollToTurn}
          />
        </div>

        <AnimatePresence>
          {away && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              className="pointer-events-none absolute inset-x-0 bottom-[7.5rem] z-sticky flex justify-center"
            >
              <IconButton
                label="回到最新"
                className="pointer-events-auto h-9 w-9 rounded-full bg-raised shadow-float"
                onClick={() => {
                  stick.current = true;
                  setAway(false);
                  markSessionRead(sessionId);
                  const el = scroller.current;
                  el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
                }}
              >
                <ArrowDown size={16} />
              </IconButton>
            </motion.div>
          )}
        </AnimatePresence>

        <Composer
          sessionId={sessionId}
          workspaceId={workspaceId}
          busy={busy}
          contextPercent={percent}
          onCancel={() => void cancel()}
          onSubmit={async (text: string, level: PermissionLevel, confirmed: boolean) => {
            const task = await submit(sessionId, text, level, confirmed);
            if (task) void client.invalidateQueries({ queryKey: ['tasks'] });
          }}
        />
      </div>

      {narrow ? (
        <AnimatePresence>
          {drawerOpen && (
            <>
              <motion.div
                key="scrim"
                aria-hidden
                className="fixed inset-0 z-drawer bg-canvas/50 backdrop-blur-sm"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                onClick={toggleInspector}
              />
              <motion.aside
                key="drawer"
                aria-label="检视器"
                className="fixed inset-y-0 right-0 z-modal w-[min(352px,92vw)] border-l border-ink/[0.07] bg-canvas/95 shadow-float backdrop-blur-2xl dark:border-white/[0.08]"
                initial={{ x: 360 }}
                animate={{ x: 0 }}
                exit={{ x: 360 }}
                transition={SPRING}
              >
                <Inspector sessionId={sessionId} />
              </motion.aside>
            </>
          )}
        </AnimatePresence>
      ) : (
        <motion.aside
          aria-label="检视器"
          initial={false}
          animate={{ width: inspectorOpen ? 340 : 0, opacity: inspectorOpen ? 1 : 0 }}
          transition={SPRING}
          className="relative z-sticky shrink-0 overflow-hidden border-l border-ink/[0.05] dark:border-white/[0.05]"
        >
          <div className="h-full w-[340px]">
            {inspectorOpen && <Inspector sessionId={sessionId} />}
          </div>
        </motion.aside>
      )}
    </div>
  );
}
