/** 自绘复选框：方形描边 + 勾；整行可点，键盘空格切换。 */
import { AnimatePresence, motion } from 'motion/react';
import { Check } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '../../utils/text';

export function Checkbox({
  checked,
  onChange,
  label,
  children,
  disabled,
  tone = 'normal',
  className,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  /** 无障碍名称；有 children 时仍用 label 作为 aria-label */
  label: string;
  children?: ReactNode;
  disabled?: boolean;
  tone?: 'normal' | 'danger';
  className?: string;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={label}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'flex w-full items-start gap-2 rounded-lg text-left text-[12px] leading-snug outline-none',
        'focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50',
        tone === 'danger' ? 'text-bad' : 'text-ink',
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          'mt-px flex h-[16px] w-[16px] shrink-0 items-center justify-center rounded-[5px] border transition-colors duration-150',
          checked
            ? tone === 'danger'
              ? 'border-bad bg-bad text-white'
              : 'border-accent bg-accent text-white'
            : 'border-ink/20 bg-card',
        )}
      >
        <AnimatePresence initial={false}>
          {checked && (
            <motion.span
              initial={{ scale: 0.4, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.4, opacity: 0 }}
              transition={{ duration: 0.12 }}
            >
              <Check size={11} strokeWidth={3} />
            </motion.span>
          )}
        </AnimatePresence>
      </span>
      {children && <span className="min-w-0 flex-1">{children}</span>}
    </button>
  );
}
