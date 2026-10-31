import type { SVGProps } from 'react';
import { cn } from '../../utils/text';

export function StarLogo({
  size = 20,
  className,
  ...rest
}: { size?: number; className?: string } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={cn('shrink-0', className)}
      {...rest}
    >
      <defs>
        <linearGradient id="brand-star-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#818cf8" />
          <stop offset="50%" stopColor="#a78bfa" />
          <stop offset="100%" stopColor="#f472b6" />
        </linearGradient>
      </defs>
      <path
        d="M12 2.5 C12.6 7.4 16.6 11.4 21.5 12 C16.6 12.6 12.6 16.6 12 21.5 C11.4 16.6 7.4 12.6 2.5 12 C7.4 11.4 11.4 7.4 12 2.5 Z"
        fill="url(#brand-star-grad)"
      />
      <path
        d="M19 4 C19.2 5.2 20 6 21.2 6.2 C20 6.4 19.2 7.2 19 8.4 C18.8 7.2 18 6.4 16.8 6.2 C18 6 18.8 5.2 19 4 Z"
        fill="currentColor"
        className="text-accent-2/80 dark:text-white/90"
      />
    </svg>
  );
}

export function BrandBadge({
  collapsed = false,
  className,
}: {
  collapsed?: boolean;
  className?: string;
}) {
  return (
    <div className={cn('flex items-center gap-2 select-none', className)}>
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] p-1 shadow-sm ring-1 ring-ink/[0.06] backdrop-blur dark:bg-white/[0.06] dark:ring-white/[0.08]">
        <StarLogo size={18} />
      </div>
      {!collapsed && (
        <span className="font-semibold tracking-tight text-[14px] text-ink">
          Base<span className="text-accent font-medium">Agent</span>
        </span>
      )}
    </div>
  );
}
