import type { ReactNode } from 'react';
import { isUnsupported } from '../../api/endpoints';
import { cn } from '../../utils/text';

/** 一行设置：左侧名称与说明，右侧控件 */
export function Row({
  title,
  note,
  children,
  className,
}: {
  title: ReactNode;
  note?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex items-center gap-3 py-3', className)}>
      <div className="min-w-0 flex-1">
        <div className="text-[14px]">{title}</div>
        {note && <div className="mt-0.5 text-[12.5px] leading-snug text-mute">{note}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-2">{children}</div>
    </div>
  );
}

export function Group({
  title,
  children,
  aside,
}: {
  title?: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="mb-5">
      {(title || aside) && (
        <div className="mb-1 flex items-center px-1">
          <h2 className="eyebrow">{title}</h2>
          <span className="ml-auto">{aside}</span>
        </div>
      )}
      <div className="divide-y divide-ink/[0.06] rounded-2xl bg-card/80 px-4 shadow-card dark:divide-white/[0.07]">
        {children}
      </div>
    </section>
  );
}

/** 作用域徽标 */
export function Scope({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-md bg-ink/[0.06] px-1.5 py-px text-[10.5px] font-medium text-mute">
      {children}
    </span>
  );
}

export const UNSUPPORTED = '后端未提供';

export function supportedBy(query: { isError: boolean; error: unknown }): boolean {
  return !(query.isError && isUnsupported(query.error));
}
