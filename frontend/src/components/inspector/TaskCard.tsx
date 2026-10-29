/**
 * 右侧第一张卡片：当前任务与 Agent 规划好的里程碑。
 * 运行中取事件流归约的状态；否则取会话最新一个任务的快照。设计见 03_visual_design.md §4.3。
 */
import { useQuery } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import type { PlanItem } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { CheckDraw, OrbitGlyph, PlanGlyph } from '../icons/Glyphs';
import { Card, EASE_OUT, StatusPill } from '../ui/primitives';

function PlanList({ plan }: { plan: PlanItem[] }) {
  return (
    <ol aria-label="计划" className="space-y-0">
      {plan.map((m, i) => (
        <motion.li
          key={m.id}
          initial={{ opacity: 0, x: -6 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04, duration: 0.22 }}
          className="relative flex gap-2.5 pb-3 last:pb-0"
        >
          {i < plan.length - 1 && (
            <span
              aria-hidden
              className={cn(
                'absolute left-[7.5px] top-5 h-[calc(100%-14px)] w-px',
                m.status === 'completed' ? 'bg-ok/40' : 'bg-ink/10',
              )}
            />
          )}
          <span
            className={cn(
              'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center',
              m.status === 'completed' && 'text-ok',
              m.status === 'in_progress' && 'text-accent',
              m.status === 'pending' && 'text-mute/50',
            )}
          >
            {m.status === 'completed' ? (
              <CheckDraw size={16} />
            ) : m.status === 'in_progress' ? (
              <OrbitGlyph size={16} />
            ) : (
              <span className="h-2 w-2 rounded-full border-[1.5px] border-current" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div
              className={cn(
                'text-[13px] leading-snug',
                m.status === 'completed' && 'text-mute',
                m.status === 'in_progress' && 'font-semibold',
              )}
            >
              {m.title}
            </div>
          </div>
        </motion.li>
      ))}
    </ol>
  );
}

export function TaskCard() {
  const sessionId = useAppStore((s) => s.sessionId);
  const run = useAppStore((s) => s.run);
  const tasks = useQuery({
    queryKey: ['tasks', sessionId],
    queryFn: () => api.sessions.tasks(sessionId as string, 20),
    enabled: !!sessionId,
  });
  const latest = tasks.data?.[0];
  const view = run
    ? {
        goal: run.goal,
        status: run.status,
        plan: run.plan,
        steps: run.stepCount,
        tokens: run.totalTokens,
      }
    : latest
      ? {
          goal: latest.task_goal,
          status: latest.status,
          plan: latest.plan,
          steps: latest.step_count,
          tokens: latest.total_tokens,
        }
      : null;
  const meta = view ? statusMeta(view.status) : null;
  const done = view?.plan.filter((m) => m.status === 'completed').length ?? 0;
  const total = view?.plan.length ?? 0;

  const aside = view && (
    <div className="flex items-center gap-2">
      <span className="text-xs font-mono tabular-nums text-mute">
        {view.steps} 步{view.tokens ? ` · ${formatTokens(view.tokens)} Token` : ''}
      </span>
      {meta &&
        (view.status === 'succeeded' ? (
          <span title="已完成" aria-label="已完成" className="flex items-center text-ok">
            <CheckDraw size={18} />
          </span>
        ) : (
          <StatusPill tone={meta.tone} icon={meta.icon} live={meta.live}>
            {meta.label}
          </StatusPill>
        ))}
    </div>
  );

  return (
    <Card id="task" title="任务" aside={aside}>
      {!view ? (
        <div className="flex flex-col items-center py-5 text-center">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-ink/[0.04] text-mute mb-1 dark:bg-white/[0.06]">
            <PlanGlyph size={16} />
          </div>
          <p className="mt-1 text-xs text-mute leading-relaxed">
            {sessionId ? '还没有任务。在下方输入目标开始。' : '选择会话后显示任务'}
          </p>
        </div>
      ) : total > 0 ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between text-xs text-mute">
            <span>里程碑进度</span>
            <span className="font-semibold tabular-nums text-ink">
              {done}/{total}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-ink/[0.07] dark:bg-white/[0.08]">
            <motion.div
              className="h-full rounded-full bg-brand"
              initial={false}
              animate={{ width: `${(done / total) * 100}%` }}
              transition={{ duration: 0.5, ease: EASE_OUT }}
            />
          </div>
          <PlanList plan={view.plan} />
        </div>
      ) : null}
    </Card>
  );
}
