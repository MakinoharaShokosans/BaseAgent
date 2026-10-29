/** 思考链：流式时展开并滚动跟随，完成后折叠为一行；纯文本，按不可信内容处理。 */
import { Brain } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Chevron, Reveal } from '../../../components/ui/primitives';
import { useUiStore } from '../../../stores/useUiStore';
import type { TimelineItem } from '../../../stores/taskReducer';
import { cn, stripControl } from '../../../utils/text';
import { formatDuration } from '../../../utils/time';
import { useNow } from '../../../utils/useNow';

type Item = Extract<TimelineItem, { kind: 'reasoning' }>;

export function ReasoningBlock({ item }: { item: Item }) {
  const pref = useUiStore((s) => s.thinking);
  const [open, setOpen] = useState(pref === 'expanded');
  const touched = useRef(false);
  const body = useRef<HTMLDivElement>(null);
  const now = useNow(item.streaming);

  // 流式结束后自动收起一次；用户手动操作过就不再干预
  useEffect(() => {
    if (!item.streaming && !touched.current) setOpen(false);
  }, [item.streaming]);

  useEffect(() => {
    if (item.streaming && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [item.text, item.streaming]);

  if (pref === 'hidden') return null;
  const elapsed = ((item.endTs ?? now) - item.startTs) * 1000;
  // 模型一次回完整思考时没有 delta，起止会相同；这时不显示一个假的 0ms
  const duration = Math.max(0, elapsed);
  const label = item.streaming
    ? `思考中${duration >= 500 ? ` ${formatDuration(duration)}` : ''}`
    : `思考${duration >= 500 ? ` ${formatDuration(duration)}` : ''}`;

  return (
    <div className="text-[13px]">
      <button
        aria-expanded={open}
        onClick={() => {
          touched.current = true;
          setOpen((v) => !v);
        }}
        className="flex items-center gap-1.5 rounded-lg py-0.5 pr-2 text-mute transition-colors hover:text-ink"
      >
        <Brain size={14} className={cn(item.streaming && 'animate-breathe text-accent')} />
        <span className="tabular-nums">{label}</span>
        <Chevron open={open} size={12} />
      </button>
      <Reveal open={open}>
        <div
          ref={body}
          className="mt-1 max-h-72 overflow-y-auto whitespace-pre-wrap break-words border-l-2 border-accent/25 pl-3 font-mono text-[12.5px] leading-relaxed text-mute"
        >
          {stripControl(item.text)}
          {item.streaming && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-accent/70" />
          )}
        </div>
      </Reveal>
    </div>
  );
}
