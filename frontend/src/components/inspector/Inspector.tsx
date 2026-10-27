/** 右侧栏：可折叠的卡片——任务、上下文占比。见 03_visual_design.md §4.3。 */
import { ContextCard } from './ContextCard';
import { McpCard } from './McpCard';
import { TaskCard } from './TaskCard';

export function Inspector() {
  return (
    <div className="h-full w-full space-y-3 overflow-y-auto overflow-x-hidden p-3 no-scrollbar">
      <TaskCard />
      <ContextCard />
      <McpCard />
    </div>
  );
}
