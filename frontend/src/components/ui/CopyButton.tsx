import { Check, Copy } from 'lucide-react';
import { useState } from 'react';
import { IconButton } from './primitives';

export function CopyButton({
  text,
  label = '复制',
  className,
}: {
  text: string | (() => string);
  label?: string;
  className?: string;
}) {
  const [done, setDone] = useState(false);
  return (
    <IconButton
      label={done ? '已复制' : label}
      className={className ?? 'h-6 w-6'}
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard?.writeText(typeof text === 'function' ? text() : text);
        setDone(true);
        setTimeout(() => setDone(false), 1400);
      }}
    >
      {done ? <Check size={13} /> : <Copy size={13} />}
    </IconButton>
  );
}
