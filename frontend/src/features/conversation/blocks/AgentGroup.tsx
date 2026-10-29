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
          'flex w-full items-center gap-2 rounded-lg py-0.5 text-left transition-colors hover:text-ink',
          warned ? 'text-warn' : 'text-mute',
        )}
      >
        <Icon size={14} className="shrink-0" />
        <span className="min-w-0 flex-1 truncate">{last?.text}</span>
        {items.length > 1 && <span className="shrink-0 text-2xs tabular-nums">{items.length}</span>}
        <Chevron open={open} size={12} />
      </button>
      <Reveal open={open}>
        <ul className="mt-1 space-y-1 border-l-2 border-ink/[0.08] pl-3">
          {items.map((i) => (
            <li
              key={i.seq}
              className={cn(
                'break-words text-[12.5px] leading-snug',
                i.tone === 'warn' ? 'font-medium text-warn' : 'text-mute',
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
