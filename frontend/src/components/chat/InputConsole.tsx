import { ArrowUp, Shield, ShieldAlert, ShieldCheck } from 'lucide-react';
import { motion } from 'motion/react';
import { useState, type ReactNode } from 'react';
import type { PermissionLevel } from '../../types/api';
import { cn } from '../../utils/text';
import { Select } from '../ui/Select';
import { Button, Kbd, Modal } from '../ui/primitives';

const LEVELS: { value: PermissionLevel; label: string; hint: string }[] = [
  { value: 'read_only', label: '只读', hint: '只能查看，不会修改任何文件' },
  {
    value: 'workspace_write',
    label: '工作区读写',
    hint: '可在工作区内修改文件、构建、测试；联网、安装、推送需要确认',
  },
  {
    value: 'full_permissions',
    label: '完全权限',
    hint: '不再请求确认，包括联网、安装、推送、系统级命令',
  },
];

export interface InputConsoleProps {
  disabled?: boolean;
  /** 任务进行中：输入台显示流光边框 */
  busy?: boolean;
  placeholder?: string;
  onSubmit: (goal: string, level: PermissionLevel, confirmedFull: boolean) => void | Promise<void>;
  /** 受控输入（可选）：空状态下的建议按钮用它预填 */
  value?: string;
  onValueChange?: (value: string) => void;
  /** 底部一行最左侧的插槽：模型下拉 */
  leading?: ReactNode;
}

const LEVEL_ICON = {
  read_only: Shield,
  workspace_write: ShieldCheck,
  full_permissions: ShieldAlert,
} as const;

export function InputConsole({
  disabled,
  busy,
  placeholder,
  onSubmit,
  value,
  onValueChange,
  leading,
}: InputConsoleProps) {
  const [inner, setInner] = useState('');
  const goal = value ?? inner;
  const setGoal = (v: string) => (onValueChange ? onValueChange(v) : setInner(v));
  // full_permissions 不会被记住：每次任务都要重新确认
  const [level, setLevel] = useState<PermissionLevel>('workspace_write');
  const [asking, setAsking] = useState(false);
  const hint = LEVELS.find((l) => l.value === level)?.hint ?? '';
  const LevelIcon = LEVEL_ICON[level];
  const danger = level === 'full_permissions';

  async function send(confirmed: boolean) {
    const text = goal.trim();
    if (!text) return;
    await onSubmit(text, level, confirmed);
    setGoal('');
    setLevel('workspace_write');
    setAsking(false);
  }

  const ready = !disabled && !!goal.trim();

  return (
    <form
      className="mx-auto w-full max-w-[760px] px-5 pb-5"
      onSubmit={(e) => {
        e.preventDefault();
        if (danger) setAsking(true);
        else void send(false);
      }}
    >
      {/* 无边框浮层；运行中外圈一道缓慢旋转的极简环境漫射光 */}
      <div
        className={cn(
          'relative rounded-[26px] p-[1.5px] transition-shadow duration-300',
          busy
            ? 'animate-[spin-border_4s_linear_infinite] bg-[conic-gradient(from_var(--spin),rgb(var(--c-accent)/0.8),rgb(var(--c-accent-2)/0.8),transparent_50%,transparent_80%,rgb(var(--c-accent)/0.8))]'
            : danger
              ? 'bg-bad/30'
              : 'bg-transparent',
        )}
      >
        <div className="rounded-[24.5px] bg-card/95 backdrop-blur-2xl p-3.5 shadow-float border border-ink/[0.08] dark:border-white/[0.09] transition-[box-shadow,background-color] duration-300 focus-within:shadow-[0_20px_50px_-12px_rgb(var(--c-accent)/0.25),0_0_0_2px_rgb(var(--c-accent)/0.35)]">
          <textarea
            aria-label="任务目标"
            className="max-h-48 min-h-[3.5rem] w-full resize-none bg-transparent px-2.5 pt-1 text-[15px] leading-relaxed outline-none placeholder:text-mute/50 disabled:cursor-not-allowed no-scrollbar"
            rows={2}
            maxLength={20000}
            disabled={disabled}
            placeholder={
              busy
                ? 'Agent 正在工作，完成后可以继续提问…'
                : (placeholder ?? '描述你想让 Agent 完成的任务…')
            }
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey))
                e.currentTarget.form?.requestSubmit();
            }}
          />
          <div className="mt-1.5 flex items-center gap-2">
            {leading}
            <Select
              label="权限基线"
              value={level}
              onChange={setLevel}
              placement="up"
              title={hint}
              className={cn(
                danger && 'bg-bad/10 text-bad hover:bg-bad/15 border-bad/20',
                level === 'read_only' && 'bg-ok/10 text-ok hover:bg-ok/15 border-ok/20',
              )}
              leading={<LevelIcon size={13} />}
              options={LEVELS.map((l) => ({
                value: l.value,
                label: l.label,
                hint: l.hint,
                icon: (() => {
                  const Icon = LEVEL_ICON[l.value];
                  return <Icon size={14} className="text-mute" />;
                })(),
              }))}
            />
            <span className="min-w-0 flex-1" />
            <span className="hidden items-center gap-1 sm:flex text-mute/50 font-mono text-xs">
              <Kbd>Ctrl</Kbd>
              <Kbd>↵</Kbd>
            </span>
            <motion.button
              type="submit"
              aria-label="发送"
              title="发送"
              disabled={!ready}
              whileTap={ready ? { scale: 0.92 } : undefined}
              animate={{ rotate: ready ? 0 : -90 }}
              transition={{ type: 'spring', stiffness: 420, damping: 26 }}
              className={cn(
                'flex h-9 w-9 items-center justify-center rounded-full text-white transition-[filter,box-shadow,opacity] duration-150',
                ready
                  ? 'bg-brand shadow-[0_4px_16px_-2px_rgb(var(--c-accent)/0.5)] hover:brightness-110 active:scale-95'
                  : 'bg-ink/10 text-mute dark:bg-white/10',
              )}
            >
              <ArrowUp size={18} strokeWidth={2.5} />
            </motion.button>
          </div>
        </div>
      </div>
      {asking && (
        <Modal title="以完全权限运行？" onClose={() => setAsking(false)}>
          <p className="text-sm text-ink">
            此任务中<strong>所有操作将自动执行，不再询问</strong>
            ，包括联网、安装依赖、推送代码和系统级命令。 这个选择不会被记住，每次任务都要重新确认。
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button autoFocus onClick={() => setAsking(false)}>
              取消
            </Button>
            <Button variant="danger" onClick={() => void send(true)}>
              我确认，以完全权限运行
            </Button>
          </div>
        </Modal>
      )}
    </form>
  );
}
