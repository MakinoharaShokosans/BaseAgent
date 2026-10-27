/**
 * 对话节点轨道：每一轮一个圆点，当前可见的那一轮放大并用品牌渐变；
 * 悬停显示这一轮用户消息的摘要，点击平滑滚动过去。设计见 03_visual_design.md §6.4。
 */
import { AnimatePresence, motion } from 'motion/react';
import { useRef, useState } from 'react';
import { cn } from '../../utils/text';

export interface RailNode {
  id: string;
  label: string;
  live?: boolean;
}

export function NodeRail({
  nodes,
  active,
  onJump,
}: {
  nodes: RailNode[];
  active: string | null;
  onJump: (id: string) => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  // 摘要气泡放在滚动列表之外，否则会被列表的 overflow 裁掉
  const [hover, setHover] = useState<{ id: string; index: number; top: number } | null>(null);

  function show(id: string, index: number, el: HTMLElement) {
    const box = wrap.current?.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    setHover({ id, index, top: box ? rect.top - box.top + rect.height / 2 : 0 });
  }

  const label = hover ? nodes.find((n) => n.id === hover.id)?.label : undefined;
  return (
    <div ref={wrap} className="absolute right-3 top-1/2 z-10 max-h-[60%] -translate-y-1/2">
      <nav aria-label="对话节点" className="max-h-full overflow-y-auto py-2 no-scrollbar">
        <ol className="flex flex-col items-center gap-1">
          {nodes.map((node, index) => {
            const current = node.id === active;
            return (
              <li key={node.id}>
                <button
                  aria-label={`跳到第 ${index + 1} 轮`}
                  aria-current={current || undefined}
                  className="flex h-5 w-5 items-center justify-center rounded-full"
                  onMouseEnter={(e) => show(node.id, index, e.currentTarget)}
                  onMouseLeave={() => setHover(null)}
                  onFocus={(e) => show(node.id, index, e.currentTarget)}
                  onBlur={() => setHover(null)}
                  onClick={() => onJump(node.id)}
                >
                  <motion.span
                    animate={{ scale: current ? 1 : hover?.id === node.id ? 0.85 : 0.6 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                    className={cn(
                      'block h-2.5 w-2.5 rounded-full',
                      current
                        ? 'bg-brand shadow-[0_0_0_3px_rgb(221_42_123/0.15)]'
                        : node.live
                          ? 'animate-pulse bg-accent'
                          : 'bg-ink/25',
                    )}
                  />
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
      <AnimatePresence>
        {hover && label !== undefined && (
          <span
            key={hover.id}
            style={{ top: hover.top }}
            className="pointer-events-none absolute right-full mr-2 -translate-y-1/2"
          >
            {/* motion 的 transform 会覆盖 translate 类，所以定位与动画分两层 */}
            <motion.span
              initial={{ opacity: 0, x: 6 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: 4 }}
              transition={{ duration: 0.14 }}
              className="block w-max max-w-[16rem] truncate rounded-xl bg-raised/95 px-3 py-1.5 text-xs shadow-float backdrop-blur-xl"
            >
              <span className="mr-1.5 tabular-nums text-mute">{hover.index + 1}</span>
              {label}
            </motion.span>
          </span>
        )}
      </AnimatePresence>
    </div>
  );
}
