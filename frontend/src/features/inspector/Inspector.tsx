/** 右侧栏：当前会话的任务与上下文。 */
import { ContextCard } from './ContextCard';
import { TaskCard } from './TaskCard';

export function Inspector({ sessionId }: { sessionId: string }) {
  return (
    <div className="h-full w-full space-y-3 overflow-y-auto overflow-x-hidden p-3">
      <TaskCard sessionId={sessionId} />
      <ContextCard sessionId={sessionId} />
    </div>
  );
}
