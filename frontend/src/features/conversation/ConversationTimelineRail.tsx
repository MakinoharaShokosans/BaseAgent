/** 对话区右侧时间轴节点导航：平滑滚动、最多 10 个节点滑动窗口、上下垂直三点省略。 */
import { useMemo } from 'react';
import { Tip } from '../../components/ui/Tooltip';
import { cn } from '../../utils/text';

export interface DialogueNode {
  id: string;
  goal: string;
  index: number;
}

interface ConversationTimelineRailProps {
  dialogues: DialogueNode[];
  activeIndex: number;
  onSelect: (index: number) => void;
  className?: string;
}

export const TIMELINE_MAX_NODES = 10;

/** 计算当前活跃位置下的 10 节点滑动窗口与首尾省略号状态 */
export function computeTimelineWindow(
  total: number,
  activeIndex: number,
  windowSize = TIMELINE_MAX_NODES,
) {
  if (total <= windowSize) {
    return {
      start: 0,
      end: Math.max(0, total - 1),
      hasTopEllipsis: false,
      hasBottomEllipsis: false,
    };
  }

  // 尽量让 activeIndex 居于窗口中间
  const half = Math.floor(windowSize / 2);
  let start = activeIndex - half;
  if (start < 0) {
    start = 0;
  } else if (start + windowSize > total) {
    start = total - windowSize;
  }
  const end = start + windowSize - 1;

  return {
    start,
    end,
    hasTopEllipsis: start > 0,
    hasBottomEllipsis: end < total - 1,
  };
}

export function ConversationTimelineRail({
  dialogues,
  activeIndex,
  onSelect,
  className,
}: ConversationTimelineRailProps) {
  const total = dialogues.length;

  const { start, end, hasTopEllipsis, hasBottomEllipsis } = useMemo(
    () => computeTimelineWindow(total, activeIndex, TIMELINE_MAX_NODES),
    [total, activeIndex],
  );

  // 对话数不足 2 轮时不显示导航轨
  if (total < 2) return null;

  const visibleNodes = dialogues.slice(start, end + 1);

  return (
    <nav
      aria-label="对话时间轴导航"
      className={cn(
        'pointer-events-auto absolute right-2.5 top-1/2 z-sticky hidden -translate-y-1/2 select-none flex-col items-center sm:flex',
        className,
      )}
    >
      {/* 垂直灰色导轨线 */}
      <div
        className="pointer-events-none absolute inset-y-3 w-[1.5px] bg-ink/[0.12] dark:bg-white/[0.14]"
        aria-hidden
      />

      {/* 顶部省略号（更早的对话） */}
      {hasTopEllipsis && (
        <Tip label={`查看更早对话（前 ${start} 轮）`} side="left">
          <button
            onClick={() => onSelect(Math.max(0, start - 5))}
            className="group relative z-10 my-1 flex flex-col items-center gap-0.5 rounded-full p-1 text-mute transition-colors hover:text-ink focus-visible:outline-none"
            aria-label="跳转至更早对话"
          >
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
          </button>
        </Tip>
      )}

      {/* 动态 10 节点序列 */}
      <div className="flex flex-col items-center py-0.5">
        {visibleNodes.map((node) => {
          const isActive = node.index === activeIndex;
          const preview = node.goal ? node.goal.slice(0, 36) : `第 ${node.index + 1} 轮`;

          return (
            <Tip
              key={node.id}
              label={
                <div className="flex max-w-[240px] items-center gap-1.5 text-xs font-normal">
                  <span className="font-mono text-2xs text-mute/80">#{node.index + 1}</span>
                  <span className="truncate">{preview}</span>
                </div>
              }
              side="left"
            >
              <button
                onClick={() => onSelect(node.index)}
                aria-label={`第 ${node.index + 1} 轮对话: ${preview}`}
                aria-current={isActive ? 'true' : undefined}
                className="group relative z-10 flex h-6 w-6 items-center justify-center rounded-full focus-visible:outline-none"
              >
                <span
                  className={cn(
                    'rounded-full transition-all duration-200',
                    isActive
                      ? 'h-3 w-3 scale-110 border-2 border-canvas bg-ink/80 shadow-sm ring-2 ring-ink/20 dark:bg-white dark:ring-white/30'
                      : 'h-2.5 w-2.5 border-[1.5px] border-ink/30 bg-canvas group-hover:scale-125 group-hover:border-ink/80 dark:border-white/35 dark:group-hover:border-white',
                  )}
                />
              </button>
            </Tip>
          );
        })}
      </div>

      {/* 底部省略号（更新的对话） */}
      {hasBottomEllipsis && (
        <Tip label={`查看更新对话（后 ${total - 1 - end} 轮）`} side="left">
          <button
            onClick={() => onSelect(Math.min(total - 1, end + 5))}
            className="group relative z-10 my-1 flex flex-col items-center gap-0.5 rounded-full p-1 text-mute transition-colors hover:text-ink focus-visible:outline-none"
            aria-label="跳转至更新对话"
          >
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
            <span className="h-1 w-1 rounded-full bg-ink/35 transition-colors group-hover:bg-ink dark:bg-white/40 dark:group-hover:bg-white" />
          </button>
        </Tip>
      )}
    </nav>
  );
}
