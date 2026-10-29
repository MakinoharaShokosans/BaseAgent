/** 自绘开关与分段选择。 */
import { motion } from 'motion/react';
import type { ReactNode } from 'react';
import { cn } from '../../utils/text';
import { Tip } from './Tooltip';

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
  tip,
  className,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
  /** 悬停气泡；默认用 label */
  tip?: ReactNode;
  className?: string;
}) {
  return (
    <Tip label={tip ?? label}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        aria-disabled={disabled || undefined}
        onClick={() => {
          if (!disabled) onChange(!checked);
        }}
        className={cn(
          'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full border border-ink/[0.06] p-[2px] transition-colors duration-200',
          checked ? 'bg-brand' : 'bg-ink/[0.12]',
          // 不用原生 disabled：那会让悬停气泡收不到事件，也会失去键盘焦点
          'aria-disabled:cursor-not-allowed aria-disabled:opacity-40',
          className,
        )}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 500, damping: 32 }}
          className={cn(
            'block h-[16px] w-[16px] rounded-full bg-white shadow-[0_1px_4px_rgb(0_0_0/0.28)]',
            checked ? 'ml-auto' : 'ml-0',
          )}
        />
      </button>
    </Tip>
  );
}

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  icon?: ReactNode;
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  className,
}: {
  value: T;
  onChange: (value: T) => void;
  options: SegmentOption<T>[];
  label: string;
  className?: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        'inline-flex rounded-full border border-ink/[0.06] bg-ink/[0.05] p-0.5',
        className,
      )}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(o.value)}
            className={cn(
              'relative inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-colors',
              on ? 'text-ink' : 'text-mute hover:text-ink',
            )}
          >
            {on && (
              <motion.span
                layoutId={`seg-${label}`}
                className="absolute inset-0 rounded-full bg-raised shadow-card"
                transition={{ type: 'spring', stiffness: 420, damping: 34 }}
              />
            )}
            <span className="relative flex items-center gap-1.5">
              {o.icon}
              {o.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
