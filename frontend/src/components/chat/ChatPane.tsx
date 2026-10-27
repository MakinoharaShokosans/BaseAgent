/**
 * 对话区：每一轮（用户消息 + 回复）是一个节点，右缘的节点轨道可点击跳转；
 * 滚动条隐藏但滚轮照常可用。设计见 docs/10_frontend/03_visual_design.md §6。
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { Bug, GitPullRequest, Map as MapIcon, RotateCcw, Square } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import { TERMINAL_STATUSES, type Task, type Turn } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { HitlApprovalCard } from '../domain/HitlApprovalCard';
import { SafeMarkdown } from '../common/SafeMarkdown';
import { Button, Chevron, EASE_OUT, Reveal, StatusPill } from '../ui/primitives';
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
}

/** 把对话流水按"用户消息 + 其后的回复"切成轮次 */
function toRounds(turns: Turn[]): Round[] {
  const rounds: Round[] = [];
  for (const t of turns) {
    if (t.role === 'user' || !rounds.length) {
      rounds.push({ id: `turn-${t.id}`, user: t.role === 'user' ? t.content : '', replies: [] });
      if (t.role === 'user') continue;
    }
    (rounds[rounds.length - 1] as Round).replies.push(t);
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
      <div className="max-w-[85%] whitespace-pre-wrap rounded-[20px] rounded-br-md bg-ink/[0.055] px-4 py-2.5 text-[15px] leading-relaxed">
        {children}
      </div>
    </div>
  );
}

function Thinking() {
  return (
    <span className="inline-flex items-center gap-1 text-sm text-mute" aria-label="正在处理">
      正在处理
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1 w-1 animate-dots rounded-full bg-current"
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
      <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.6, ease: EASE_OUT }}
          aria-hidden
          className="h-14 w-14 rounded-full bg-brand opacity-90 blur-[1px]"
        />
        <motion.p {...enter} className="mt-3 font-display text-[30px] leading-tight">
          在左侧选择一个会话，或新建一个会话开始
        </motion.p>
        <motion.p
          {...enter}
          transition={{ ...enter.transition, delay: 0.08 }}
          className="max-w-sm text-sm text-mute"
        >
          {workspaceId
            ? '点工作区右侧的 + 新建会话。每个会话有自己的对话记录与记忆。'
            : '没有工作区的话，先在左侧添加一个项目目录。Agent 只能在这个目录里读写。'}
        </motion.p>
      </div>
    );
  }

  const meta = run ? statusMeta(run.status) : null;
  const empty = !run && !rounds.length;
  const steps = run?.items.length ?? 0;

  return (
    <div className="relative flex h-full flex-col">
      <div ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto no-scrollbar">
        <div className="mx-auto w-full max-w-[760px] space-y-10 px-6 pb-12 pt-6">
          {empty && (
            <div className="flex flex-col items-center pt-[10vh] text-center">
              <motion.div
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.6, ease: EASE_OUT }}
                aria-hidden
                className="h-14 w-14 rounded-full bg-brand opacity-90"
              />
              <motion.h2
                {...enter}
                className="mt-7 font-display text-[42px] leading-[1.05] tracking-tight"
              >
                想让 Agent <em className="text-brand">做点什么</em>？
              </motion.h2>
              <motion.p
                {...enter}
                transition={{ ...enter.transition, delay: 0.06 }}
                className="mt-3 max-w-md text-mute"
              >
                它会在你的工作区里读代码、改文件、跑命令，需要越权时会先问你。
              </motion.p>
              <div className="mt-9 grid w-full max-w-xl gap-2.5 sm:grid-cols-3">
                {SUGGESTIONS.map((s, i) => (
                  <motion.button
                    key={s.text}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.12 + i * 0.05, duration: 0.3, ease: EASE_OUT }}
                    whileHover={{ y: -3 }}
                    whileTap={{ scale: 0.98 }}
                    className="group flex flex-col gap-2 rounded-[20px] bg-card p-4 text-left shadow-card transition-shadow hover:shadow-float"
                    onClick={() => setDraft(s.text)}
                  >
                    <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink/[0.05] text-ink transition-colors group-hover:bg-brand group-hover:text-white">
                      {s.icon}
                    </span>
                    <span className="text-[13px] font-medium leading-snug">{s.text}</span>
                    <span className="text-2xs text-mute">{s.hint}</span>
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
              className="scroll-mt-6 space-y-5"
            >
              {r.user && <UserBubble>{r.user}</UserBubble>}
              {r.replies.map((t) => (
                <SafeMarkdown key={t.id} text={t.content} />
              ))}
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
                <div className="rounded-[20px] bg-card/70 shadow-card backdrop-blur">
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
