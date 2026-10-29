/**
 * 自绘下拉：触发按钮 + 渲染到 body 的列表（z-pop），不被祖先的 overflow 裁切，
 * 可在弹窗、抽屉、滚动容器里正常使用。键盘：方向键、Enter、Esc、Tab。
 */
import { AnimatePresence, motion } from 'motion/react';
import { Check, ChevronDown } from 'lucide-react';
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../../utils/text';
import { Tip } from './Tooltip';
import { useAnchor } from './useAnchor';

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  hint?: string;
  icon?: ReactNode;
}

interface SelectProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: SelectOption<T>[];
  /** 无障碍名称，同时作为悬停气泡 */
  label: string;
  placement?: 'up' | 'down';
  align?: 'start' | 'end';
  disabled?: boolean;
  /** 触发按钮上替代选项图标的前导元素 */
  leading?: ReactNode;
  /** 触发按钮上只显示图标，不显示文字 */
  iconOnly?: boolean;
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
  iconOnly,
  className,
  menuClassName,
  title,
}: SelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLUListElement>(null);
  const listId = useId();
  const current = options.find((o) => o.value === value);
  const place = useAnchor(open, trigger, menu, placement === 'up' ? 'top' : 'bottom', align);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!trigger.current?.contains(t) && !menu.current?.contains(t)) setOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    const close = () => setOpen(false);
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', close);
    };
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
    if (disabled) return;
    if (!open) {
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(e.key)) {
        e.preventDefault();
        show();
      }
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
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

  const up = place ? place.side === 'top' : placement === 'up';
  const button = (
    <button
      ref={trigger}
      type="button"
      aria-label={label}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={open ? listId : undefined}
      aria-disabled={disabled || undefined}
      onKeyDown={onKey}
      className={cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium text-ink transition-[background-color,transform] active:scale-[0.98]',
        'border border-ink/[0.06] bg-ink/[0.045] hover:bg-ink/[0.08] aria-disabled:cursor-not-allowed aria-disabled:opacity-50',
        open && 'bg-ink/[0.08]',
        iconOnly && 'h-8 w-8 justify-center px-0',
        className,
      )}
      onClick={() => (open ? setOpen(false) : show())}
    >
      {leading ?? current?.icon}
      {!iconOnly && <span className="truncate">{current?.label ?? '—'}</span>}
      {!iconOnly && (
        <ChevronDown
          size={12}
          aria-hidden
          className={cn(
            'shrink-0 opacity-60 transition-transform duration-200',
            open && 'rotate-180',
          )}
        />
      )}
    </button>
  );

  return (
    <>
      <Tip
        disabled={open}
        label={title ?? (iconOnly ? `${label}：${current?.label ?? ''}` : (current?.hint ?? ''))}
      >
        {button}
      </Tip>
      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.ul
              ref={menu}
              id={listId}
              role="listbox"
              aria-label={label}
              initial={{ opacity: 0, scale: 0.96, y: up ? 6 : -6 }}
              animate={{ opacity: place ? 1 : 0, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: up ? 4 : -4 }}
              transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
              style={{
                position: 'fixed',
                top: place?.top ?? 0,
                left: place?.left ?? 0,
                minWidth: Math.max(place?.width ?? 0, 176),
                transformOrigin: `${align === 'end' ? 'right' : 'left'} ${up ? 'bottom' : 'top'}`,
              }}
              className={cn(
                'z-pop max-h-72 max-w-[min(360px,calc(100vw-16px))] overflow-y-auto rounded-[16px] border border-ink/[0.07] bg-raised/95 p-1.5 shadow-float backdrop-blur-2xl dark:border-white/[0.08]',
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
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
