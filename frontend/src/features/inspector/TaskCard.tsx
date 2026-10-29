import { useQuery } from '@tanstack/react-query';
import {
  Check,
  Clock,
  Cpu,
  Download,
  FileText,
  Loader2,
  Play,
  RotateCw,
  Square,
  Wrench,
  X,
} from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { CopyButton } from '../../components/ui/CopyButton';
import { Card, IconButton, Modal } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import { TERMINAL_STATUSES, type Artifact, type PlanItem } from '../../types/api';
import { statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { formatBytes, formatDuration } from '../../utils/time';
import { useNow } from '../../utils/useNow';

function Plan({ plan }: { plan: PlanItem[] }) {
  const scrollToTurn = (index: number) => {
    const el =
      document.getElementById(`turn-item-${index + 1}`) ||
      document.querySelector(`[data-turn="${index + 1}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  };

  return (
    <ol className="space-y-1">
      {plan.map((p, idx) => (
        <li
          key={p.id}
          onClick={() => scrollToTurn(idx)}
          className={cn(
            'group flex cursor-pointer items-start gap-2 rounded-lg px-1.5 py-1 text-[13px] leading-snug transition-colors hover:bg-ink/[0.04] dark:hover:bg-white/[0.04]',
            p.status === 'completed' && 'text-mute line-through decoration-mute/40',
            p.status === 'pending' && 'text-mute',
          )}
          title="点击跳转至对应回合"
        >
          {p.status === 'completed' ? (
            <Check size={13} strokeWidth={2.5} className="mt-1 shrink-0 text-ok" />
          ) : p.status === 'in_progress' ? (
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent animate-pulse" />
          ) : (
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-mute/30" />
          )}
          <span className="min-w-0 break-words group-hover:text-ink dark:group-hover:text-white">
            {p.title}
          </span>
        </li>
      ))}
    </ol>
  );
}

function MinimalStatusMark({
  status,
  meta,
}: {
  status: string;
  meta: ReturnType<typeof statusMeta>;
}) {
  if (status === 'succeeded') {
    return (
      <span
        className="inline-flex shrink-0 items-center justify-center text-ok"
        title="已完成"
        aria-label="已完成"
      >
        <Check size={16} strokeWidth={2.5} />
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <span
        className="inline-flex shrink-0 items-center justify-center text-bad"
        title="失败"
        aria-label="失败"
      >
        <X size={16} strokeWidth={2.5} />
      </span>
    );
  }
  if (status === 'cancelled') {
    return (
      <span
        className="inline-flex shrink-0 items-center justify-center text-mute"
        title="已取消"
        aria-label="已取消"
      >
        <Square size={12} fill="currentColor" />
      </span>
    );
  }
  if (meta.live) {
    return (
      <span
        className="inline-flex shrink-0 items-center justify-center text-accent"
        title={meta.label}
        aria-label={meta.label}
      >
        <Loader2 size={15} className="animate-spin" />
      </span>
    );
  }
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center text-warn"
      title={meta.label}
      aria-label={meta.label}
    >
      <Clock size={15} />
    </span>
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
  const [viewing, setViewing] = useState<Artifact | null>(null);
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

  // 过滤内部命令增量/输出记录（如 step_1_bash），仅保留用户真实关注的生成产物
  const isInternalArtifact = (id: string) =>
    /^step_\d+/.test(id) || id.includes('bash');

  const deliverables = (artifacts.data ?? []).filter(
    (a) => !isInternalArtifact(a.artifact_id),
  );

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
        </span>
      }
    >
      {run && meta ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2">
            <p className="line-clamp-3 min-w-0 flex-1 break-words text-[13px] leading-snug">
              {run.goal}
            </p>
            <MinimalStatusMark status={run.status} meta={meta} />
          </div>

          {run.plan.length > 0 && <Plan plan={run.plan} />}

          <div className="flex items-center justify-between py-1 text-[12.5px]">
            <div className="flex items-center gap-1.5" title={`回合数：${run.stepCount}`}>
              <RotateCw size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">{run.stepCount}</span>
            </div>
            <div className="flex items-center gap-1.5" title={`工具调用：${toolCount}`}>
              <Wrench size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">{toolCount}</span>
            </div>
            <div
              className="flex items-center gap-1.5"
              title={`Token 消耗：${formatTokens(run.totalTokens)}`}
            >
              <Cpu size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">
                {formatTokens(run.totalTokens)}
              </span>
            </div>
            <div
              className="flex items-center gap-1.5"
              title={`用时：${elapsed !== null ? formatDuration(Math.max(0, elapsed)) : '—'}`}
            >
              <Clock size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">
                {elapsed !== null ? formatDuration(Math.max(0, elapsed)) : '—'}
              </span>
            </div>
          </div>

          {deliverables.length > 0 && (
            <ul className="space-y-0.5 text-[12.5px]">
              {deliverables.map((a) => (
                <li key={a.artifact_id}>
                  <button
                    className="group -mx-1 flex w-[calc(100%+0.5rem)] items-center gap-2 rounded-lg px-1.5 py-1 text-left font-mono text-mute hover:bg-ink/[0.045] hover:text-ink"
                    onClick={() => setViewing(a)}
                    aria-label={`查看产物 ${a.artifact_id}`}
                  >
                    <FileText
                      size={13}
                      className="shrink-0 text-accent/70 group-hover:text-accent"
                    />
                    <span className="min-w-0 flex-1 truncate">{a.artifact_id}</span>
                    <span className="shrink-0 tabular-nums text-2xs text-mute/60">
                      {formatBytes(a.size)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="py-2 text-center text-mute">—</p>
      )}

      {viewing && run && (
        <ArtifactViewer taskId={run.taskId} artifact={viewing} onClose={() => setViewing(null)} />
      )}
    </Card>
  );
}
