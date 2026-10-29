import type { TaskStatus } from '../../types/api';
import { cn } from '../../utils/text';

const LABEL: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  waiting_for_approval: '等待审批',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  terminated: '已终止',
};

export function statusLabel(status: TaskStatus | null | undefined): string {
  return status ? (LABEL[status] ?? status) : '空闲';
}

/** 状态点：形状 + 颜色 + 动效共同表达，不只靠颜色 */
export function StatusDot({
  status,
  size = 8,
  className,
}: {
  status: TaskStatus | null | undefined;
  size?: number;
  className?: string;
}) {
  const live = status === 'running' || status === 'queued';
  const tone =
    status === 'waiting_for_approval'
      ? 'bg-warn'
      : status === 'failed' || status === 'terminated'
        ? 'bg-bad'
        : live
          ? 'bg-accent'
          : status === 'succeeded'
            ? 'bg-ok/80'
            : '';
  return (
    <span
      role="img"
      aria-label={statusLabel(status)}
      style={{ width: size, height: size }}
      className={cn(
        'relative inline-block shrink-0 rounded-full',
        tone || 'border border-mute/50',
        live && 'animate-halo',
        status === 'waiting_for_approval' && 'animate-halo [--tw-shadow-color:rgb(var(--c-warn))]',
        className,
      )}
    />
  );
}
