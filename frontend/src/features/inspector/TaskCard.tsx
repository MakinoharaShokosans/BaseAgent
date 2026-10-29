import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CheckCircle2,
  Circle,
  CircleDot,
  Download,
  Eye,
  History,
  Loader2,
  Play,
  Square,
} from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { useSessionTasks } from '../../api/queries';
import { CopyButton } from '../../components/ui/CopyButton';
import { Card, IconButton, Modal, StatusPill } from '../../components/ui/primitives';
import { Reveal } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import { TERMINAL_STATUSES, type Artifact, type PlanItem, type Task } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { formatBytes, formatDuration, relativeTime } from '../../utils/time';
import { useNow } from '../../utils/useNow';

const PLAN_ICON = { completed: CheckCircle2, in_progress: CircleDot, pending: Circle } as const;

function Plan({ plan }: { plan: PlanItem[] }) {
  return (
    <ol className="space-y-1">
      {plan.map((p) => {
        const Icon = PLAN_ICON[p.status];
        return (
          <li
            key={p.id}
            className={cn(
              'flex items-start gap-2 text-[13px] leading-snug',
              p.status === 'completed' && 'text-mute line-through decoration-mute/40',
              p.status === 'pending' && 'text-mute',
            )}
          >
            <Icon
              size={14}
              className={cn(
                'mt-0.5 shrink-0',
                p.status === 'in_progress' && 'text-accent',
                p.status === 'completed' && 'text-ok',
              )}
            />
            <span className="min-w-0 break-words">{p.title}</span>
          </li>
        );
      })}
    </ol>
  );
}

function ArtifactViewer({
  taskId,
  artifact,
  onClose,
}: {
  taskId: string;
  artifact: Artifact;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ['artifact', taskId, artifact.artifact_id],
    queryFn: () => api.tasks.artifact(taskId, artifact.artifact_id),
    retry: false,
  });
  const download = () => {
    if (q.data === undefined) return;
    const url = URL.createObjectURL(new Blob([q.data], { type: 'text/plain;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = artifact.artifact_id;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <Modal title={artifact.artifact_id} onClose={onClose} wide>
      <div className="mb-2 flex items-center gap-2 text-xs text-mute">
        <span className="tabular-nums">{formatBytes(artifact.size)}</span>
        <span className="flex-1" />
        {q.data !== undefined && <CopyButton text={q.data} label="复制产物" />}
        <IconButton label="下载" disabled={q.data === undefined} onClick={download}>
          <Download size={14} />
        </IconButton>
      </div>
      {q.isPending && <Loader2 size={18} className="mx-auto animate-spin text-mute" />}
      {q.isError && <p className="text-sm text-bad">{(q.error as Error).message}</p>}
      {q.data !== undefined && (
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
          {q.data}
        </pre>
      )}
    </Modal>
  );
}

export function TaskCard({ sessionId }: { sessionId: string }) {
  const run = useAppStore((s) => (s.runSession === sessionId ? s.run : null));
  const cancel = useAppStore((s) => s.cancel);
  const resume = useAppStore((s) => s.resume);
  const attach = useAppStore((s) => s.attach);
  const tasks = useSessionTasks(sessionId);
  const [history, setHistory] = useState(false);
  const [viewing, setViewing] = useState<Artifact | null>(null);
  const client = useQueryClient();
  const live = !!run && !TERMINAL_STATUSES.includes(run.status);
  const now = useNow(live);
  const meta = run ? statusMeta(run.status) : null;
  const toolCount = run ? run.items.filter((item) => item.kind === 'tool').length : 0;
  const artifacts = useQuery({
    queryKey: ['artifacts', run?.taskId, run?.status],
    queryFn: () => api.tasks.artifacts(run?.taskId as string),
    enabled: !!run && TERMINAL_STATUSES.includes(run.status),
  });
  const elapsed = run?.startedTs ? ((run.finishedTs ?? now) - run.startedTs) * 1000 : null;

  return (
    <Card
      id="task"
      title="任务"
      aside={
        <span className="flex items-center">
          {live && (
            <IconButton
              label="取消任务"
              className="h-7 w-7 text-bad hover:text-bad"
              onClick={() => void cancel()}
            >
              <Square size={13} fill="currentColor" />
            </IconButton>
          )}
          {run && !live && run.status !== 'succeeded' && (
            <IconButton label="从检查点续跑" className="h-7 w-7" onClick={() => void resume()}>
              <Play size={14} />
            </IconButton>
          )}
          <IconButton
            label="任务历史"
            className="h-7 w-7"
            active={history}
            onClick={() => setHistory((v) => !v)}
          >
            <History size={14} />
          </IconButton>
        </span>
      }
    >
      {run && meta ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2">
            <p className="line-clamp-3 min-w-0 flex-1 break-words text-[13px] leading-snug">
              {run.goal}
            </p>
            <StatusPill tone={meta.tone} icon={meta.icon} live={meta.live}>
              {meta.label}
            </StatusPill>
          </div>
          {run.plan.length > 0 && <Plan plan={run.plan} />}
          <dl className="grid grid-cols-4 gap-1.5 text-center">
            {[
              ['回合', String(run.stepCount)],
              ['工具', String(toolCount)],
              ['Token', formatTokens(run.totalTokens)],
              ['用时', elapsed !== null ? formatDuration(Math.max(0, elapsed)) : '—'],
            ].map(([k, v]) => (
              <div key={k} className="rounded-xl bg-ink/[0.04] px-1 py-1.5">
                <dd className="text-[13px] font-semibold tabular-nums">{v}</dd>
                <dt className="text-2xs text-mute">{k}</dt>
              </div>
            ))}
          </dl>
          {artifacts.data && artifacts.data.length > 0 && (
            <ul className="space-y-0.5 text-[12.5px]">
              {artifacts.data.map((a) => (
                <li key={a.artifact_id}>
                  <button
                    className="group -mx-1 flex w-[calc(100%+0.5rem)] items-center gap-2 rounded-lg px-1 py-0.5 text-left font-mono text-mute hover:bg-ink/[0.045] hover:text-ink"
                    onClick={() => setViewing(a)}
                    aria-label={`查看产物 ${a.artifact_id}`}
                  >
                    <Eye size={12} className="shrink-0 opacity-50 group-hover:opacity-100" />
                    <span className="min-w-0 flex-1 truncate">{a.artifact_id}</span>
                    <span className="shrink-0 tabular-nums">{formatBytes(a.size)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="flex justify-end">
            <IconButton
              label="下载轨迹"
              className="h-7 w-7"
              onClick={() => window.open(api.tasks.traceUrl(run.taskId))}
            >
              <Download size={14} />
            </IconButton>
          </div>
        </div>
      ) : (
        <p className="py-2 text-center text-mute">—</p>
      )}
      <Reveal open={history}>
        <ul className="mt-3 space-y-0.5 border-t border-ink/[0.06] pt-2">
          {(tasks.data ?? []).map((t: Task) => (
            <li key={t.task_id}>
              <button
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left text-[12.5px] hover:bg-ink/[0.05]',
                  run?.taskId === t.task_id && 'bg-ink/[0.06]',
                )}
                onClick={() => {
                  void attach(sessionId, t);
                  void client.invalidateQueries({ queryKey: ['artifacts'] });
                }}
              >
                <span className="min-w-0 flex-1 truncate">{t.task_goal}</span>
                <span className="shrink-0 text-2xs text-mute">{relativeTime(t.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </Reveal>
      {viewing && run && (
        <ArtifactViewer taskId={run.taskId} artifact={viewing} onClose={() => setViewing(null)} />
      )}
    </Card>
  );
}
