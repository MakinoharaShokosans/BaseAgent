/** 联网搜索、子智能体、上下文压缩：折叠成一行摘要，展开看明细；被拦截的事件自动展开。 */
import { Bot, Globe, Layers } from 'lucide-react';
import { useState } from 'react';
import { Chevron, Reveal } from '../../../components/ui/primitives';
import type { AgentGroup as Group, TimelineItem } from '../../../stores/taskReducer';
import { cn } from '../../../utils/text';

type Item = Extract<TimelineItem, { kind: 'agent' }>;

const ICON: Record<Group, typeof Bot> = { websearch: Globe, subagent: Bot, compaction: Layers };

export function AgentGroup({ items }: { items: Item[] }) {
  const warned = items.some((i) => i.tone === 'warn');
  const [open, setOpen] = useState(warned);
  const group = items[0]?.group ?? 'subagent';
  const Icon = ICON[group];
  const last = items[items.length - 1];
  return (
    <div className="text-[13px]">
      <button
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'group flex w-full items-center gap-2 py-0.5 text-left transition-colors hover:text-ink',
          warned ? 'text-warn' : 'text-mute',
        )}
      >
        <Icon size={14} className="shrink-0" />
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] underline decoration-ink/20 decoration-dashed underline-offset-4 group-hover:decoration-ink/50 dark:decoration-white/20 dark:group-hover:decoration-white/50">
          {last?.text}
        </span>
        {items.length > 1 && <span className="shrink-0 text-2xs tabular-nums text-mute/60">{items.length}</span>}
        <Chevron open={open} size={12} />
      </button>
      <Reveal open={open}>
        <ul className="mt-1 space-y-1 pl-5">
          {items.map((i) => (
            <li
              key={i.seq}
              className={cn(
                'break-words font-mono text-[12px] leading-snug',
                i.tone === 'warn' ? 'font-medium text-warn' : 'text-mute/80 dark:text-mute/70',
              )}
            >
              {i.text}
            </li>
          ))}
        </ul>
      </Reveal>
    </div>
  );
}
