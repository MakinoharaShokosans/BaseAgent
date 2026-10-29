/** 设置：全屏页，左侧二级导航；手机上先列出分区，再进入详情。 */
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import {
  ArrowLeft,
  Cpu,
  FileText,
  Plug,
  SlidersHorizontal,
  Sparkles,
  Stethoscope,
  Wrench,
  X,
} from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import { IconButton } from '../../components/ui/primitives';
import { usePendingStore } from '../../stores/usePendingStore';
import { useUiStore } from '../../stores/useUiStore';
import { cn } from '../../utils/text';
import { Diagnostics } from './sections/Diagnostics';
import { General } from './sections/General';
import { Mcp } from './sections/Mcp';
import { Models } from './sections/Models';
import { Prompts } from './sections/Prompts';
import { Skills } from './sections/Skills';
import { Tools } from './sections/Tools';

const SECTIONS: { id: string; label: string; icon: typeof Cpu; render: () => ReactNode }[] = [
  { id: 'general', label: '通用', icon: SlidersHorizontal, render: () => <General /> },
  { id: 'models', label: '模型', icon: Cpu, render: () => <Models /> },
  { id: 'prompts', label: '提示词', icon: FileText, render: () => <Prompts /> },
  { id: 'skills', label: '技能', icon: Sparkles, render: () => <Skills /> },
  { id: 'mcp', label: 'MCP', icon: Plug, render: () => <Mcp /> },
  { id: 'tools', label: '工具', icon: Wrench, render: () => <Tools /> },
  { id: 'diagnostics', label: '诊断', icon: Stethoscope, render: () => <Diagnostics /> },
];

export function SettingsPage() {
  const { section } = useParams({ strict: false }) as { section?: string };
  const phone = useUiStore((s) => s.phone);
  const navigate = useNavigate();
  const pending = usePendingStore((s) => s.count);
  const current = SECTIONS.find((s) => s.id === section);

  // 桌面直接进入第一个分区
  useEffect(() => {
    if (!phone && !current)
      void navigate({ to: '/settings/$section', params: { section: 'general' }, replace: true });
  }, [phone, current, navigate]);

  const nav = (
    <nav aria-label="设置分区" className="space-y-0.5 p-2">
      {SECTIONS.map((s) => {
        const on = s.id === current?.id;
        return (
          <Link
            key={s.id}
            to="/settings/$section"
            params={{ section: s.id }}
            aria-current={on ? 'page' : undefined}
            className={cn(
              'flex items-center gap-2.5 rounded-xl px-3 py-2 text-[13.5px] transition-colors',
              on
                ? 'bg-ink/[0.07] font-medium text-ink'
                : 'text-mute hover:bg-ink/[0.045] hover:text-ink',
            )}
          >
            <s.icon size={16} />
            {s.label}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-ink/[0.06] px-3 dark:border-white/[0.06] sm:px-4">
        {phone && current ? (
          <Link to="/settings">
            <IconButton label="返回" tabIndex={-1}>
              <ArrowLeft size={17} />
            </IconButton>
          </Link>
        ) : null}
        <h1 className="text-[15px] font-semibold">{phone && current ? current.label : '设置'}</h1>
        <span className="flex-1" />
        {pending > 0 && (
          <span
            role="status"
            className="rounded-full bg-warn/12 px-2.5 py-0.5 text-2xs font-semibold text-warn"
          >
            {pending} 项下个任务生效
          </span>
        )}
        <Link to="/">
          <IconButton label="关闭" tabIndex={-1}>
            <X size={17} />
          </IconButton>
        </Link>
      </header>
      <div className="flex min-h-0 flex-1">
        {(!phone || !current) && (
          <div
            className={cn(
              'shrink-0 overflow-y-auto border-ink/[0.06] dark:border-white/[0.06]',
              phone ? 'flex-1' : 'w-52 border-r',
            )}
          >
            {nav}
          </div>
        )}
        {(!phone || current) && (
          <div className="min-w-0 flex-1 overflow-y-auto">
            <div
              className={cn(
                'mx-auto w-full p-3 sm:p-6',
                current?.id === 'prompts'
                  ? 'max-w-[1560px] 2xl:max-w-[1760px]'
                  : 'max-w-[1080px]',
              )}
            >
              {current?.render()}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
