/**
 * 滑动条：凡是可以数值化的设置都用它。已选段是品牌渐变，拇指按下放大并弹出数值气泡。
 * 键盘：←→↑↓ 一步，Page Up/Down 十步，Home/End 到两端。设计见 03_visual_design.md §5。
 *
 * `scale="log"`：位置按对数分布，适合跨几个数量级的范围（如 4k–1M 的上下文上限），
 * 小数值一段占到足够的长度。对数刻度下键盘按位置走：一步是轨道的 1%，Page Up/Down 是 10%。
 */
import { AnimatePresence, motion } from 'motion/react';
import { useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { cn } from '../../utils/text';

export interface SliderProps {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  label: string;
  format?: (value: number) => string;
  disabled?: boolean;
  className?: string;
  scale?: 'linear' | 'log';
}

export function Slider({
  value,
  onChange,
  min,
  max,
  step = 1,
  label,
  format = String,
  disabled,
  className,
  scale = 'linear',
}: SliderProps) {
  const track = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  // 对数刻度要求下界为正；不满足时退回线性
  const log = scale === 'log' && min > 0 && max > min;
  const clamp01 = (r: number) => Math.min(Math.max(r, 0), 1);
  const toRatio = (v: number) =>
    clamp01(log ? Math.log(v / min) / Math.log(max / min) : (v - min) / Math.max(max - min, 1));
  const fromRatio = (r: number) =>
    log ? min * Math.pow(max / min, clamp01(r)) : min + clamp01(r) * (max - min);
  const ratio = toRatio(value);

  const snap = (raw: number) => {
    const stepped = Math.round((raw - min) / step) * step + min;
    return Math.min(Math.max(stepped, min), max);
  };

  function fromPointer(clientX: number) {
    const rect = track.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    onChange(snap(fromRatio((clientX - rect.left) / rect.width)));
  }

  /** 对数刻度下按位置挪动；吸附后若原地不动，至少走一个 step */
  function nudge(delta: number): number {
    const moved = snap(fromRatio(ratio + delta));
    if (moved !== value) return moved;
    return snap(value + Math.sign(delta) * step);
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (disabled) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    setDragging(true);
    fromPointer(e.clientX);
  }

  function onKey(e: KeyboardEvent) {
    const big = step * 10;
    const next: Record<string, number> = log
      ? {
          ArrowRight: nudge(0.01),
          ArrowUp: nudge(0.01),
          ArrowLeft: nudge(-0.01),
          ArrowDown: nudge(-0.01),
          PageUp: nudge(0.1),
          PageDown: nudge(-0.1),
          Home: min,
          End: max,
        }
      : {
          ArrowRight: value + step,
          ArrowUp: value + step,
          ArrowLeft: value - step,
          ArrowDown: value - step,
          PageUp: value + big,
          PageDown: value - big,
          Home: min,
          End: max,
        };
    if (e.key in next) {
      e.preventDefault();
      onChange(snap(next[e.key] as number));
    }
  }

  return (
    <div
      ref={track}
      className={cn(
        'relative flex h-6 w-full touch-none select-none items-center',
        disabled ? 'opacity-50' : 'cursor-pointer',
        className,
      )}
      onPointerDown={onPointerDown}
      onPointerMove={(e) => dragging && fromPointer(e.clientX)}
      onPointerUp={() => setDragging(false)}
      onPointerCancel={() => setDragging(false)}
    >
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink/[0.08]">
        <div className="h-full rounded-full bg-brand" style={{ width: `${ratio * 100}%` }} />
      </div>
      <div
        role="slider"
        tabIndex={disabled ? -1 : 0}
        aria-label={label}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-valuetext={format(value)}
        aria-disabled={disabled || undefined}
        onKeyDown={onKey}
        className="group absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full outline-none"
        style={{ left: `${ratio * 100}%` }}
      >
        <motion.span
          animate={{ scale: dragging ? 1.25 : 1 }}
          transition={{ type: 'spring', stiffness: 500, damping: 28 }}
          className="block h-[18px] w-[18px] rounded-full bg-white shadow-[0_2px_8px_rgb(0_0_0/0.18),0_0_0_0.5px_rgb(0_0_0/0.06)] group-focus-visible:ring-4 group-focus-visible:ring-accent/25"
        />
        <AnimatePresence>
          {dragging && (
            <span className="pointer-events-none absolute bottom-full left-1/2 mb-2 -translate-x-1/2">
              <motion.span
                initial={{ opacity: 0, y: 4, scale: 0.9 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 4, scale: 0.9 }}
                transition={{ duration: 0.14 }}
                className="block whitespace-nowrap rounded-lg bg-ink px-2 py-0.5 text-2xs font-semibold tabular-nums text-canvas shadow-float"
              >
                {format(value)}
              </motion.span>
            </span>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
