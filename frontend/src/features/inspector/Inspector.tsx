/** 右侧栏：当前会话的任务与上下文。 */
import { ContextCard } from './ContextCard';
import { TaskCard } from './TaskCard';

export function Inspector({ sessionId }: { sessionId: string }) {
  return (
    <div className="h-full w-full space-y-5 overflow-y-auto overflow-x-hidden p-4">
      <TaskCard sessionId={sessionId} />
      <div className="border-t border-ink/[0.06] dark:border-white/[0.06]" />
      <ContextCard sessionId={sessionId} />
    </div>
  );
}
