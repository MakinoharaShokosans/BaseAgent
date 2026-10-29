/**
 * 自定义下拉：替代原生 <select>。无边框胶囊触发器 + 毛玻璃弹层。
 * 键盘：Enter / Space / ↓ 打开，↑↓ 移动，Enter 选择，Esc 关闭并还焦点，Tab 关闭。
 * 设计见 docs/10_frontend/03_visual_design.md §6.2。
 */
import { AnimatePresence, motion } from 'motion/react';
import { Check, ChevronDown } from 'lucide-react';
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from '../../utils/text';

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  hint?: string;
  icon?: ReactNode;
}

export interface SelectProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: SelectOption<T>[];
  /** 可访问名称（触发器的 aria-label） */
  label: string;
  placement?: 'up' | 'down';
  align?: 'start' | 'end';
  disabled?: boolean;
  /** 触发器上的前置内容（图标、色点） */
  leading?: ReactNode;
  className?: string;
  menuClassName?: string;
  title?: string;
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  label,
  placement = 'down',
  align = 'start',
  disabled,
  leading,
  className,
  menuClassName,
  title,
}: SelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const current = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  function show() {
    if (disabled) return;
    setActive(
      Math.max(
        options.findIndex((o) => o.value === value),
        0,
      ),
    );
    setOpen(true);
  }

  function choose(index: number) {
    const option = options[index];
    if (option) onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  }

  function onKey(e: KeyboardEvent) {
    if (!open) {
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(e.key)) {
        e.preventDefault();
        show();
      }
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => (i + 1) % options.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => (i - 1 + options.length) % options.length);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      choose(active);
    } else if (e.key === 'Tab') {
      setOpen(false);
    }
  }

  const up = placement === 'up';
  return (
    <div ref={root} className="relative inline-flex" onKeyDown={onKey}>
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        title={title ?? current?.hint}
        disabled={disabled}
        className={cn(
          'inline-flex max-w-full items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium text-ink transition-[background-color,transform] active:scale-[0.98]',
          'bg-ink/[0.045] hover:bg-ink/[0.08] disabled:cursor-not-allowed disabled:opacity-50 border border-ink/[0.06]',
          open && 'bg-ink/[0.08]',
          className,
        )}
        onClick={() => (open ? setOpen(false) : show())}
      >
        {leading ?? current?.icon}
        <span className="truncate">{current?.label ?? '—'}</span>
        <ChevronDown
          size={12}
          aria-hidden
          className={cn(
            'shrink-0 opacity-60 transition-transform duration-200',
            open && 'rotate-180',
          )}
        />
      </button>
      <AnimatePresence>
        {open && (
          <motion.ul
            id={listId}
            role="listbox"
            aria-label={label}
            initial={{ opacity: 0, scale: 0.96, y: up ? 6 : -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: up ? 4 : -4 }}
            transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
            style={{
              transformOrigin: `${align === 'end' ? 'right' : 'left'} ${up ? 'bottom' : 'top'}`,
            }}
            className={cn(
              'absolute z-50 max-h-72 min-w-[12rem] overflow-y-auto rounded-[16px] bg-raised/95 p-1.5 shadow-float backdrop-blur-2xl border border-ink/[0.07] dark:border-white/[0.08] no-scrollbar',
              up ? 'bottom-full mb-2' : 'top-full mt-2',
              align === 'end' ? 'right-0' : 'left-0',
              menuClassName,
            )}
          >
            {options.map((option, index) => {
              const selected = option.value === value;
              return (
                <li
                  key={option.value}
                  role="option"
                  aria-selected={selected}
                  className={cn(
                    'flex cursor-pointer items-start gap-2 rounded-[11px] px-2.5 py-1.5 text-[13px] transition-colors',
                    index === active ? 'bg-ink/[0.06]' : 'hover:bg-ink/[0.04]',
                  )}
                  onMouseEnter={() => setActive(index)}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => choose(index)}
                >
                  {option.icon && <span className="mt-0.5 shrink-0">{option.icon}</span>}
                  <span className="min-w-0 flex-1">
                    <span className={cn('block truncate', selected && 'font-semibold')}>
                      {option.label}
                    </span>
                    {option.hint && (
                      <span className="mt-0.5 block text-2xs leading-snug text-mute">
                        {option.hint}
                      </span>
                    )}
                  </span>
                  <Check
                    size={14}
                    aria-hidden
                    className={cn('mt-0.5 shrink-0 text-accent', !selected && 'invisible')}
                  />
                </li>
              );
            })}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
