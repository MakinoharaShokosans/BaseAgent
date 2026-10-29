/**
 * 对话区：每一轮（用户消息 + 回复）是一个节点，右缘的节点轨道可点击跳转；
 * 滚动条隐藏但滚轮照常可用。设计见 docs/10_frontend/03_visual_design.md §6。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import {
  Bug,
  Check,
  Code2,
  Copy,
  FolderPlus,
  GitBranch,
  GitPullRequest,
  Map as MapIcon,
  Plus,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  Square,
  Terminal,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import { TERMINAL_STATUSES, type Task, type Turn } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { HitlApprovalCard } from '../domain/HitlApprovalCard';
import { SafeMarkdown } from '../common/SafeMarkdown';
import { Button, Chevron, EASE_OUT, IconButton, Reveal, StatusPill } from '../ui/primitives';
import { CompactButton } from './CompactButton';
import { InputConsole } from './InputConsole';
import { ModelPicker } from './ModelPicker';
import { NodeRail, type RailNode } from './NodeRail';
import { TraceTimeline } from './TraceTimeline';

const SUGGESTIONS: { text: string; icon: ReactNode; hint: string }[] = [
  { text: '帮我梳理一下这个工程的结构和入口', icon: <MapIcon size={16} />, hint: '只读浏览' },
  { text: '找出这个报错的原因并修复', icon: <Bug size={16} />, hint: '定位与修改' },
  { text: '给最近的改动做一次代码评审', icon: <GitPullRequest size={16} />, hint: '审阅 diff' },
];

const enter = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.28, ease: EASE_OUT },
};

interface Round {
  id: string;
  user: string;
  replies: Turn[];
  /** 这一轮最后一条流水的 id：从此处分支时，分支包含到它为止 */
  lastTurnId: number;
}

/** 把对话流水按"用户消息 + 其后的回复"切成轮次 */
function toRounds(turns: Turn[]): Round[] {
  const rounds: Round[] = [];
  for (const t of turns) {
    if (t.role === 'user' || !rounds.length) {
      rounds.push({
        id: `turn-${t.id}`,
        user: t.role === 'user' ? t.content : '',
        replies: [],
        lastTurnId: t.id,
      });
      if (t.role === 'user') continue;
    }
    const round = rounds[rounds.length - 1] as Round;
    round.replies.push(t);
    round.lastTurnId = t.id;
  }
  return rounds;
}

/**
 * 滚动容器内的平滑滚动：自己用 requestAnimationFrame 驱动，曲线与界面动效一致（ease-out，420ms）。
 * 不依赖浏览器的 smooth 行为（部分环境不生效）；减少动态效果时直接跳到位。
 */
function glide(container: HTMLElement, target: number): void {
  const top = Math.max(0, Math.min(target, container.scrollHeight - container.clientHeight));
  const reduced =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const from = container.scrollTop;
  if (reduced || typeof requestAnimationFrame === 'undefined' || from === top) {
    container.scrollTop = top;
    return;
  }
  let start: number | null = null;
  const step = (now: number) => {
    start ??= now;
    const t = Math.min((now - start) / 420, 1);
    const eased = 1 - Math.pow(1 - t, 4);
    container.scrollTop = from + (top - from) * eased;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  // 页面在后台时浏览器不派发动画帧：短时间内没开始就直接跳到位，保证点击一定生效
  setTimeout(() => {
    if (start === null) container.scrollTop = top;
  }, 150);
}

function UserBubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-[22px] rounded-br-[6px] bg-accent/[0.08] dark:bg-accent/[0.14] px-4.5 py-3 text-[14.5px] leading-relaxed text-ink border border-accent/20 shadow-sm">
        {children}
      </div>
    </div>
  );
}

/** 一轮对话下方的操作：复制这一轮的回复、从这里分出一个新会话 */
function RoundActions({
  text,
  onBranch,
  branching,
}: {
  text: string;
  onBranch: () => void;
  branching: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    const fail = () => useAppStore.getState().notify('复制失败，浏览器不允许访问剪贴板', 'error');
    if (!navigator.clipboard) return fail();
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }, fail);
  };
  return (
    <div className="-mt-2 flex items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover/round:opacity-100">
      <IconButton label={copied ? '已复制' : '复制回复'} onClick={copy}>
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </IconButton>
      <IconButton label="从此处分支" disabled={branching} onClick={onBranch}>
        <GitBranch size={14} />
      </IconButton>
    </div>
  );
}

function Thinking() {
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-mute font-medium"
      aria-label="正在处理"
    >
      正在处理
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1 w-1 animate-dots rounded-full bg-accent"
          style={{ animationDelay: `${i * 0.16}s` }}
        />
      ))}
    </span>
  );
}

export function ChatPane() {
  const sessionId = useAppStore((s) => s.sessionId);
  const workspaceId = useAppStore((s) => s.workspaceId);
  const run = useAppStore((s) => s.run);
  const connection = useAppStore((s) => s.connection);
  const { submit, approve, reject, resume, cancel, attach } = useAppStore.getState();
  const client = useQueryClient();
  const scroller = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const roundEls = useRef(new Map<string, HTMLElement>());
  const [draft, setDraft] = useState('');
  const [traceOpen, setTraceOpen] = useState(true);
  const [active, setActive] = useState<string | null>(null);
  const { selectSession, notify } = useAppStore.getState();

  const branch = useMutation({
    mutationFn: (fromTurnId: number) => api.sessions.fork(sessionId as string, fromTurnId),
    onSuccess: async (s) => {
      await client.invalidateQueries({ queryKey: ['sessions', s.workspace_id] });
      selectSession(s.session_id);
      notify('已从此处创建分支会话');
    },
    onError: (e) => notify((e as Error).message, 'error'),
  });

  const newSession = useMutation({
    mutationFn: (wId: string) => api.sessions.create(wId),
    onSuccess: async (s) => {
      await client.invalidateQueries({ queryKey: ['sessions', s.workspace_id] });
      selectSession(s.session_id);
    },
    onError: (e) => notify((e as Error).message, 'error'),
  });

  const turns = useQuery({
    queryKey: ['turns', sessionId],
    queryFn: () => api.sessions.turns(sessionId as string, 50),
    enabled: !!sessionId,
  });
  const tasks = useQuery({
    queryKey: ['tasks', sessionId],
    queryFn: () => api.sessions.tasks(sessionId as string, 20),
    enabled: !!sessionId,
  });

  // 刷新页面后恢复：会话里最新的任务若还在运行或等待审批，重新接上事件流
  const latest: Task | undefined = tasks.data?.[0];
  useEffect(() => {
    if (!run && latest && !TERMINAL_STATUSES.includes(latest.status)) void attach(latest);
  }, [run, latest, attach]);

  const busy = !!run && !TERMINAL_STATUSES.includes(run.status);

  useEffect(() => {
    if (run && TERMINAL_STATUSES.includes(run.status)) {
      void client.invalidateQueries({ queryKey: ['turns', sessionId] });
      void client.invalidateQueries({ queryKey: ['tasks', sessionId] });
      void client.invalidateQueries({ queryKey: ['smemory', sessionId] });
      void client.invalidateQueries({ queryKey: ['context', sessionId] });
    }
  }, [run?.status, run, client, sessionId]);

  // 运行时时间线默认展开，结束后自动收起，只留结论
  useEffect(() => setTraceOpen(busy), [busy, run?.taskId]);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: 'end', behavior: 'smooth' });
  }, [run?.lastSeq, turns.data?.length]);

  // 当前任务的轮次由事件流渲染；流水里属于它的轮次不重复显示
  const rounds = useMemo(
    () => toRounds((turns.data ?? []).filter((t) => !run || t.task_id !== run.taskId)),
    [turns.data, run],
  );
  const nodes: RailNode[] = useMemo(
    () => [
      ...rounds.map((r) => ({ id: r.id, label: r.user || '（无用户消息）' })),
      ...(run ? [{ id: `run-${run.taskId}`, label: run.goal, live: busy }] : []),
    ],
    [rounds, run, busy],
  );

  const register = useCallback(
    (id: string) => (el: HTMLElement | null) => {
      if (el) roundEls.current.set(id, el);
      else roundEls.current.delete(id);
    },
    [],
  );

  // 当前轮：IntersectionObserver 判定，不逐帧监听滚动
  useEffect(() => {
    const root = scroller.current;
    if (!root || typeof IntersectionObserver === 'undefined') return;
    const ratios = new Map<string, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries)
          ratios.set((e.target as HTMLElement).dataset.round ?? '', e.intersectionRatio);
        let best: string | null = null;
        let max = 0;
        for (const n of nodes) {
          const r = ratios.get(n.id) ?? 0;
          if (r > max) [best, max] = [n.id, r];
        }
        if (best) setActive(best);
      },
      { root, threshold: [0, 0.25, 0.5, 0.75, 1] },
    );
    for (const el of roundEls.current.values()) observer.observe(el);
    return () => observer.disconnect();
  }, [nodes]);

  const jump = (id: string) => {
    const el = roundEls.current.get(id);
    if (el && scroller.current) glide(scroller.current, el.offsetTop - 24);
  };

  if (!sessionId) {
    return (
      <div className="relative flex h-full flex-col items-center justify-center overflow-y-auto px-6 py-10 text-center no-scrollbar">
        {/* 中心微发光徽标 */}
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.6, ease: EASE_OUT }}
          className="relative flex h-14 w-14 items-center justify-center rounded-2xl bg-brand text-white shadow-glow ring-1 ring-white/25"
        >
          <Sparkles size={24} className="text-white drop-shadow" />
          <span className="absolute -inset-2 rounded-3xl bg-brand opacity-20 blur-xl animate-pulse" />
        </motion.div>

        {/* 标题（严格保留测试断言字符串） */}
        <motion.h2
          {...enter}
          className="mt-5 text-2xl sm:text-3xl font-bold tracking-tight text-ink"
        >
          在左侧选择一个会话，或新建一个会话开始
        </motion.h2>

        {/* 说明 */}
        <motion.p
          {...enter}
          transition={{ ...enter.transition, delay: 0.08 }}
          className="mt-2 max-w-md text-sm text-mute leading-relaxed"
        >
          {workspaceId
            ? '点工作区右侧的 + 新建会话。每个会话有自己独立的上下文与记忆。'
            : '没有工作区的话，先在左侧添加一个项目目录。Agent 只能在工作区内读写。'}
        </motion.p>

        {/* 快捷操作 CTA 按钮 */}
        {workspaceId ? (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.4 }}
            className="mt-5"
          >
            <button
              onClick={() => newSession.mutate(workspaceId)}
              disabled={newSession.isPending}
              className="inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white shadow-float transition-all hover:brightness-105 active:scale-95 disabled:opacity-50"
            >
              <Plus size={16} strokeWidth={2.5} />
              {newSession.isPending ? '正在创建…' : '立即新建会话并进入'}
            </button>
          </motion.div>
        ) : (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.4 }}
            className="mt-5"
          >
            <button
              onClick={() => {
                const addBtn = document.querySelector(
                  'button[aria-label="添加工作区"]',
                ) as HTMLButtonElement | null;
                addBtn?.click();
              }}
              className="inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-semibold text-white shadow-float transition-all hover:brightness-105 active:scale-95"
            >
              <FolderPlus size={16} strokeWidth={2} />
              添加第一个工作区
            </button>
          </motion.div>
        )}

        {/* Bento Grid 平台能力概览模块 */}
        <div className="mt-10 grid w-full max-w-2xl grid-cols-1 gap-3.5 sm:grid-cols-2 text-left">
          <div className="bento-card p-4.5 border border-ink/[0.06] dark:border-white/[0.07]">
            <div className="flex items-center justify-between mb-2.5">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/10 text-accent">
                <Code2 size={15} />
              </span>
              <span className="rounded-md bg-ink/[0.04] dark:bg-white/[0.06] px-1.5 py-0.5 text-2xs font-mono text-mute">
                AST & Context
              </span>
            </div>
            <h3 className="text-[13.5px] font-semibold text-ink">工程全景深度感知</h3>
            <p className="mt-1 text-xs text-mute leading-relaxed">
              多文件 AST 分析与符号索引，自主理解复杂项目的入口结构与依赖链路。
            </p>
          </div>

          <div className="bento-card p-4.5 border border-ink/[0.06] dark:border-white/[0.07]">
            <div className="flex items-center justify-between mb-2.5">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-ok/10 text-ok">
                <ShieldCheck size={15} />
              </span>
              <span className="rounded-md bg-ink/[0.04] dark:bg-white/[0.06] px-1.5 py-0.5 text-2xs font-mono text-mute">
                HITL Sandbox
              </span>
            </div>
            <h3 className="text-[13.5px] font-semibold text-ink">细粒度沙箱安全隔离</h3>
            <p className="mt-1 text-xs text-mute leading-relaxed">
              严格受限于工作区目录。网络外联与高危指令强制触发人机协同审批确认。
            </p>
          </div>

          <div className="bento-card p-4.5 border border-ink/[0.06] dark:border-white/[0.07]">
            <div className="flex items-center justify-between mb-2.5">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent-2/10 text-accent-2">
                <GitBranch size={15} />
              </span>
              <span className="rounded-md bg-ink/[0.04] dark:bg-white/[0.06] px-1.5 py-0.5 text-2xs font-mono text-mute">
                Fork & Memory
              </span>
            </div>
            <h3 className="text-[13.5px] font-semibold text-ink">检查点分叉与记忆追踪</h3>
            <p className="mt-1 text-xs text-mute leading-relaxed">
              支持在历史任意回复节点开辟新分支，长程推理轨迹与上下文记忆精准续跑。
            </p>
          </div>

          <div className="bento-card p-4.5 border border-ink/[0.06] dark:border-white/[0.07]">
            <div className="flex items-center justify-between mb-2.5">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-warn/10 text-warn">
                <Terminal size={15} />
              </span>
              <span className="rounded-md bg-ink/[0.04] dark:bg-white/[0.06] px-1.5 py-0.5 text-2xs font-mono text-mute">
                MCP Ecosystem
              </span>
            </div>
            <h3 className="text-[13.5px] font-semibold text-ink">模型与 MCP 协议总线</h3>
            <p className="mt-1 text-xs text-mute leading-relaxed">
              支持动态切换顶尖大模型，无缝集成 MCP 协议工具，自由扩展代码分析能力。
            </p>
          </div>
        </div>
      </div>
    );
  }

  const meta = run ? statusMeta(run.status) : null;
  const empty = !run && !rounds.length;
  const steps = run ? run.items.length : 0;

  return (
    <div className="relative flex h-full flex-col">
      <div ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto no-scrollbar">
        <div className="mx-auto w-full max-w-[760px] space-y-10 px-6 pb-12 pt-6">
          {empty && (
            <div className="flex flex-col items-center pt-[6vh] text-center">
              <motion.div
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.5, ease: EASE_OUT }}
                className="inline-flex items-center gap-2 rounded-full border border-accent/20 bg-accent/5 px-3.5 py-1 text-xs font-semibold text-accent shadow-sm"
              >
                <span className="h-2 w-2 rounded-full bg-ok shadow-[0_0_8px_rgb(var(--c-ok)/0.8)]" />
                智能体就绪 · 安全沙箱隔离
              </motion.div>
              <motion.h2
                {...enter}
                className="mt-6 text-3xl sm:text-4xl font-bold tracking-tight text-ink"
              >
                想让 Agent <span className="text-brand">做点什么</span>？
              </motion.h2>
              <motion.p
                {...enter}
                transition={{ ...enter.transition, delay: 0.06 }}
                className="mt-2.5 max-w-lg text-[14px] leading-relaxed text-mute"
              >
                在安全沙箱与工作区边界内，自主读写代码、运行测试、排查问题。需要越权时会先征求你的审批。
              </motion.p>
              <div className="mt-8 grid w-full max-w-2xl gap-3.5 sm:grid-cols-3">
                {SUGGESTIONS.map((s, i) => (
                  <motion.button
                    key={s.text}
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.1 + i * 0.06, duration: 0.35, ease: EASE_OUT }}
                    whileHover={{ y: -3, scale: 1.01 }}
                    whileTap={{ scale: 0.98 }}
                    className="group flex flex-col justify-between gap-3.5 rounded-[22px] bg-card p-4 text-left shadow-card border border-ink/[0.06] transition-[box-shadow,border-color] duration-200 hover:border-accent/30 hover:shadow-float dark:border-white/[0.07]"
                    onClick={() => setDraft(s.text)}
                  >
                    <div className="flex items-center justify-between">
                      <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-ink/[0.04] text-ink transition-colors group-hover:bg-brand group-hover:text-white dark:bg-white/[0.06]">
                        {s.icon}
                      </span>
                      <span className="rounded-md bg-ink/[0.04] dark:bg-white/[0.06] px-1.5 py-0.5 text-2xs text-mute font-medium">
                        {s.hint}
                      </span>
                    </div>
                    <span className="text-[13.5px] font-semibold leading-snug text-ink">
                      {s.text}
                    </span>
                  </motion.button>
                ))}
              </div>
            </div>
          )}

          {rounds.map((r) => (
            <motion.section
              key={r.id}
              ref={register(r.id)}
              data-round={r.id}
              {...enter}
              className="group/round scroll-mt-6 space-y-5"
            >
              {r.user && <UserBubble>{r.user}</UserBubble>}
              {r.replies.map((t) => (
                <SafeMarkdown key={t.id} text={t.content} />
              ))}
              {r.replies.length > 0 && (
                <RoundActions
                  text={r.replies.map((t) => t.content).join('\n\n')}
                  branching={branch.isPending}
                  onBranch={() => branch.mutate(r.lastTurnId)}
                />
              )}
            </motion.section>
          ))}

          {run && (
            <motion.section
              ref={register(`run-${run.taskId}`)}
              data-round={`run-${run.taskId}`}
              {...enter}
              className="scroll-mt-6 space-y-4"
            >
              <UserBubble>{run.goal}</UserBubble>
              <div className="flex flex-wrap items-center gap-2">
                {meta && (
                  <StatusPill tone={meta.tone} icon={meta.icon} live={meta.live}>
                    {meta.label}
                  </StatusPill>
                )}
                {connection === 'reconnecting' && (
                  <StatusPill tone="amber">连接中断，正在重连…</StatusPill>
                )}
                {busy && (
                  <Button
                    variant="ghost"
                    className="px-2.5 py-1 text-xs"
                    onClick={() => void cancel()}
                  >
                    <Square size={11} fill="currentColor" /> 取消
                  </Button>
                )}
                {(run.status === 'terminated' || run.status === 'failed') && (
                  <Button className="px-2.5 py-1 text-xs" onClick={() => void resume()}>
                    <RotateCcw size={13} /> 从检查点续跑
                  </Button>
                )}
              </div>

              {steps > 0 && (
                <div className="overflow-hidden rounded-[22px] bg-card/75 shadow-card backdrop-blur-md border border-ink/[0.06] dark:border-white/[0.07]">
                  <button
                    className="flex w-full items-center gap-2 px-4 py-3 text-left text-[13px] font-semibold text-mute transition-colors hover:text-ink"
                    aria-expanded={traceOpen}
                    onClick={() => setTraceOpen((v) => !v)}
                  >
                    <Chevron open={traceOpen} />
                    执行过程（{steps}）
                    {busy && (
                      <span className="ml-auto">
                        <Thinking />
                      </span>
                    )}
                  </button>
                  <Reveal open={traceOpen}>
                    <div className="px-4 pb-4 pt-1">
                      <TraceTimeline items={run.items} />
                    </div>
                  </Reveal>
                </div>
              )}
              {busy && steps === 0 && <Thinking />}

              <AnimatePresence>
                {run.approval && (
                  <motion.div
                    key={run.approval.approval_id}
                    initial={{ opacity: 0, scale: 0.98, y: 6 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.98 }}
                    transition={{ duration: 0.28, ease: EASE_OUT }}
                  >
                    <HitlApprovalCard
                      request={run.approval}
                      onApprove={(decision) => approve(decision)}
                      onReject={(reason) => reject(reason)}
                    />
                  </motion.div>
                )}
              </AnimatePresence>
              {run.error && (
                <p className="rounded-2xl bg-bad/10 px-4 py-2.5 text-sm text-bad">{run.error}</p>
              )}
              {run.terminationReason && (
                <p className="rounded-2xl bg-warn/10 px-4 py-2.5 text-sm text-warn">
                  终止原因：{run.terminationReason}
                </p>
              )}
              {run.delivery && (
                <motion.div {...enter}>
                  <SafeMarkdown text={run.delivery} />
                </motion.div>
              )}
            </motion.section>
          )}
          <div ref={bottom} />
        </div>
      </div>

      {nodes.length > 1 && <NodeRail nodes={nodes} active={active} onJump={jump} />}

      <InputConsole
        disabled={busy}
        busy={busy}
        value={draft}
        onValueChange={setDraft}
        leading={
          <>
            <ModelPicker sessionId={sessionId} />
            <CompactButton sessionId={sessionId} disabled={busy} />
          </>
        }
        onSubmit={(goal, level, confirmed) => submit(goal, level, confirmed)}
      />
    </div>
  );
}
