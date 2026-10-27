/**
 * 右侧第一张卡片：当前任务与 Agent 规划好的里程碑。
 * 运行中取事件流归约的状态；否则取会话最新一个任务的快照。设计见 03_visual_design.md §4.3。
 */
import { useQuery } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import type { Milestone } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { CheckDraw, CrossGlyph, OrbitGlyph, PlanGlyph } from '../icons/Glyphs';
import { Card, EASE_OUT, StatusPill } from '../ui/primitives';

function MilestoneList({ milestones }: { milestones: Milestone[] }) {
  return (
    <ol aria-label="里程碑" className="space-y-0">
      {milestones.map((m, i) => (
        <motion.li
          key={m.id}
          initial={{ opacity: 0, x: -6 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04, duration: 0.22 }}
          className="relative flex gap-2.5 pb-3 last:pb-0"
        >
          {i < milestones.length - 1 && (
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
              m.status === 'failed' && 'text-bad',
              m.status === 'pending' && 'text-mute/50',
            )}
          >
            {m.status === 'completed' ? (
              <CheckDraw size={16} />
            ) : m.status === 'in_progress' ? (
              <OrbitGlyph size={16} />
            ) : m.status === 'failed' ? (
              <CrossGlyph size={16} />
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
            {m.description && m.status === 'in_progress' && (
              <div className="mt-0.5 text-xs leading-snug text-mute">{m.description}</div>
            )}
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
        milestones: run.milestones,
        steps: run.stepCount,
        tokens: run.totalTokens,
      }
    : latest
      ? {
          goal: latest.task_goal,
          status: latest.status,
          milestones: latest.milestones,
          steps: latest.step_count,
          tokens: latest.total_tokens,
        }
      : null;
  const meta = view ? statusMeta(view.status) : null;
  const done = view?.milestones.filter((m) => m.status === 'completed').length ?? 0;
  const total = view?.milestones.length ?? 0;

  return (
    <Card
      id="task"
      title="任务"
      aside={
        meta && (
          <StatusPill tone={meta.tone} icon={meta.icon} live={meta.live}>
            {meta.label}
          </StatusPill>
        )
      }
    >
      {!view ? (
        <div className="flex flex-col items-center py-6 text-center">
          <PlanGlyph size={20} className="text-mute" />
          <p className="mt-2 text-xs text-mute">
            {sessionId ? '还没有任务。在下方输入目标开始。' : '选择会话后显示任务'}
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="line-clamp-3 text-[13px] leading-relaxed text-ink/80" title={view.goal}>
            {view.goal}
          </p>
          <div className="flex gap-4 text-xs text-mute">
            <span>
              <span className="font-semibold tabular-nums text-ink">{view.steps}</span> 步
            </span>
            <span>
              <span className="font-semibold tabular-nums text-ink">
                {formatTokens(view.tokens)}
              </span>{' '}
              Token
            </span>
            {total > 0 && (
              <span className="ml-auto">
                <span className="font-semibold tabular-nums text-ink">
                  {done}/{total}
                </span>{' '}
                里程碑
              </span>
            )}
          </div>
          {total > 0 ? (
            <>
              <div className="h-1 overflow-hidden rounded-full bg-ink/[0.07]">
                <motion.div
                  className="h-full rounded-full bg-brand"
                  initial={false}
                  animate={{ width: `${(done / total) * 100}%` }}
                  transition={{ duration: 0.5, ease: EASE_OUT }}
                />
              </div>
              <MilestoneList milestones={view.milestones} />
            </>
          ) : (
            <p className="text-xs text-mute">Agent 还没有给出计划。</p>
          )}
        </div>
      )}
    </Card>
  );
}
