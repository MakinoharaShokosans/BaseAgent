import { useEffect, useRef, useState } from 'react';
import { cn } from '../../utils/text';

/** 行内改名：Enter 提交、Esc 取消、失焦提交；内容没变化不触发回调。 */
export function InlineEdit({
  value,
  onCommit,
  onCancel,
  className,
  maxLength = 120,
  label,
}: {
  value: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
  className?: string;
  maxLength?: number;
  label: string;
}) {
  const [text, setText] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  const finish = (commit: boolean) => {
    if (done.current) return;
    done.current = true;
    const next = text.trim();
    if (commit && next && next !== value) onCommit(next);
    else onCancel();
  };
  return (
    <input
      ref={ref}
      aria-label={label}
      value={text}
      maxLength={maxLength}
      onChange={(e) => setText(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Enter') finish(true);
        else if (e.key === 'Escape') finish(false);
      }}
      onBlur={() => finish(true)}
      className={cn(
        'min-w-0 flex-1 rounded-md bg-card px-1.5 py-0.5 text-[13px] text-ink outline-none ring-1 ring-accent/40',
        className,
      )}
    />
  );
}
