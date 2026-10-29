/**
 * 输入台。任务运行期间提交的消息进入队列（可撤回、可调序），当前任务正常结束后依次提交；
 * 失败 / 取消 / 终止会暂停队列。设计见 docs/10_frontend/04_redesign.md §5.5。
 */
import { useQuery } from '@tanstack/react-query';
import {
  ArrowUp,
  ChevronDown,
  ChevronUp,
  ListPlus,
  Pause,
  Play,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Square,
  Undo2,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, isUnsupported } from '../../api/endpoints';
import { useModels, useSessionSettings } from '../../api/useSettings';
import { Select } from '../../components/ui/Select';
import { Button, IconButton, Modal } from '../../components/ui/primitives';
import { queueOf, useQueueStore } from '../../stores/useQueueStore';
import { useUiStore } from '../../stores/useUiStore';
import type { PermissionLevel } from '../../types/api';
import { cn, formatTokens } from '../../utils/text';

const LEVELS: { value: PermissionLevel; label: string; hint: string; icon: typeof Shield }[] = [
  { value: 'read_only', label: '只读', hint: '只能查看，不会修改任何文件', icon: Shield },
  {
    value: 'workspace_write',
    label: '工作区读写',
    hint: '可在工作区内修改文件、构建、测试；联网、安装、推送需要确认',
    icon: ShieldCheck,
  },
  {
    value: 'full_permissions',
    label: '完全权限',
    hint: '不再请求确认，包括联网、安装、推送、系统级命令',
    icon: ShieldAlert,
  },
];

const AUTO = '__auto__';

function ModelPicker({
  sessionId,
  workspaceModel,
}: {
  sessionId: string;
  workspaceModel: string | null;
}) {
  const models = useModels();
  const { query, save } = useSessionSettings(sessionId);
  const current = query.data?.model ?? AUTO;
  return (
    <Select
      label="模型"
      value={current}
      placement="up"
      disabled={!query.data || save.isPending}
      leading={<span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-brand" />}
      onChange={(value) =>
        save.mutate({
          model: value === AUTO ? null : value,
          context_token_limit: query.data?.context_token_limit ?? null,
        })
      }
      options={[
        {
          value: AUTO,
          label: workspaceModel ? `默认 · ${workspaceModel}` : '默认',
          hint: '按工作区设置或配置的端点顺序，失败时自动降级',
        },
        ...(models.data ?? []).map((m) => ({
          value: m.name,
          label: m.name,
          hint: `${m.model} · ${formatTokens(m.context_window)} 上下文`,
        })),
      ]}
    />
  );
}

function ContextRing({ percent, onClick }: { percent: number; onClick: () => void }) {
  const r = 8;
  const c = 2 * Math.PI * r;
  const tone = percent > 85 ? 'text-bad' : percent > 65 ? 'text-warn' : 'text-accent';
  return (
    <IconButton label={`上下文 ${Math.round(percent)}%`} onClick={onClick} side="top">
      <svg width="20" height="20" viewBox="0 0 20 20" className="-rotate-90">
        <circle cx="10" cy="10" r={r} fill="none" strokeWidth="2.2" className="stroke-ink/[0.12]" />
        <circle
          cx="10"
          cy="10"
          r={r}
          fill="none"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - Math.min(percent, 100) / 100)}
          className={cn('stroke-current transition-[stroke-dashoffset] duration-500', tone)}
        />
      </svg>
    </IconButton>
  );
}

export function Composer({
  sessionId,
  workspaceId,
  busy,
  contextPercent,
  onSubmit,
  onCancel,
}: {
  sessionId: string;
  workspaceId: string;
  /** 当前有任务在运行或等待审批 */
  busy: boolean;
  contextPercent: number | null;
  onSubmit: (text: string, level: PermissionLevel, confirmedFull: boolean) => Promise<void>;
  onCancel: () => void;
}) {
  const phone = useUiStore((s) => s.phone);
  const openCard = useUiStore((s) => s.openCard);
  const toggleInspector = useUiStore((s) => s.toggleInspector);
  const draftKey = `baseagent.draft.${sessionId}`;
  const [text, setText] = useState(() => {
    try {
      return window.localStorage.getItem(draftKey) ?? '';
    } catch {
      return '';
    }
  });
  const area = useRef<HTMLTextAreaElement>(null);
  const queue = useQueueStore((s) => queueOf(s, sessionId));
  const { enqueue, remove, move, setPaused, clear } = useQueueStore.getState();

  const wsSettings = useQuery({
    queryKey: ['wsettings', workspaceId],
    queryFn: () => api.workspaces.settings(workspaceId),
    retry: false,
  });
  const wsData = wsSettings.isError && isUnsupported(wsSettings.error) ? null : wsSettings.data;
  // 完全权限不被记住：即便工作区默认是它，也要在发送时重新确认
  const defaultLevel = wsData?.permission_level ?? 'workspace_write';
  const [level, setLevel] = useState<PermissionLevel>(defaultLevel);
  useEffect(() => setLevel(defaultLevel), [defaultLevel, sessionId]);
  const [asking, setAsking] = useState<string | null>(null);
  const danger = level === 'full_permissions';

  useEffect(() => {
    try {
      if (text) window.localStorage.setItem(draftKey, text);
      else window.localStorage.removeItem(draftKey);
    } catch {
      /* 存储不可用：草稿只在本次页面内有效 */
    }
  }, [text, draftKey]);

  // 输入框随内容长高，最多八行
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 8 * 24 + 16)}px`;
  }, [text]);

  async function send(message: string, confirmed: boolean) {
    if (busy) enqueue(sessionId, message, level);
    else await onSubmit(message, level, confirmed);
    setText('');
    setLevel(defaultLevel === 'full_permissions' ? 'workspace_write' : defaultLevel);
    setAsking(null);
  }

  function submit() {
    const message = text.trim();
    if (!message) return;
    if (danger) setAsking(message);
    else void send(message, false);
  }

  const ready = !!text.trim();
  const LevelIcon = LEVELS.find((l) => l.value === level)?.icon ?? Shield;

  return (
    <div className="mx-auto w-full max-w-[780px] px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-5 sm:pb-5">
      {queue.items.length > 0 && (
        <div className="mb-2 overflow-hidden rounded-2xl border border-ink/[0.07] bg-card/80 backdrop-blur-xl dark:border-white/[0.08]">
          <div className="flex items-center gap-1 px-3 pt-1.5 text-2xs font-semibold text-mute">
            {queue.paused ? <Pause size={12} className="text-warn" /> : <ListPlus size={12} />}
            <span className="tabular-nums">{queue.items.length}</span>
            <span className={cn('ml-1 font-normal', queue.paused ? 'text-warn' : 'text-mute/80')}>
              {queue.paused ? '队列已暂停' : '待提交'}
            </span>
            <span className="flex-1" />
            {queue.paused && (
              <IconButton
                label="继续提交队列"
                className="h-6 w-6"
                onClick={() => setPaused(sessionId, false)}
              >
                <Play size={12} />
              </IconButton>
            )}
            <IconButton label="清空队列" className="h-6 w-6" onClick={() => clear(sessionId)}>
              <Undo2 size={12} />
            </IconButton>
          </div>
          <ul className="max-h-40 overflow-y-auto px-1.5 pb-1.5">
            {queue.items.map((m, i) => (
              <li
                key={m.id}
                className="group flex items-center gap-1 rounded-lg px-1.5 py-1 text-[13px] hover:bg-ink/[0.04]"
              >
                <span className="w-4 shrink-0 text-center text-2xs tabular-nums text-mute">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{m.text}</span>
                <span className="flex shrink-0 opacity-100 sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
                  <IconButton
                    label="上移"
                    className="h-6 w-6"
                    disabled={i === 0}
                    onClick={() => move(sessionId, m.id, -1)}
                  >
                    <ChevronUp size={13} />
                  </IconButton>
                  <IconButton
                    label="下移"
                    className="h-6 w-6"
                    disabled={i === queue.items.length - 1}
                    onClick={() => move(sessionId, m.id, 1)}
                  >
                    <ChevronDown size={13} />
                  </IconButton>
                  <IconButton
                    label="撤回"
                    className="h-6 w-6"
                    onClick={() => remove(sessionId, m.id)}
                  >
                    <Undo2 size={13} />
                  </IconButton>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className={cn(
          'rounded-[24px] border bg-card/95 p-2.5 shadow-float backdrop-blur-2xl transition-shadow duration-300',
          'focus-within:shadow-[0_20px_50px_-12px_rgb(var(--c-accent)/0.25),0_0_0_2px_rgb(var(--c-accent)/0.35)]',
          danger ? 'border-bad/40' : 'border-ink/[0.08] dark:border-white/[0.09]',
        )}
      >
        <textarea
          ref={area}
          aria-label="消息"
          rows={1}
          maxLength={20000}
          value={text}
          placeholder={busy ? '排队下一条消息' : '消息'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || e.nativeEvent.isComposing) return;
            // 手机上 Enter 换行（软键盘），桌面 Enter 发送、Shift+Enter 换行
            if ((!phone && !e.shiftKey) || e.metaKey || e.ctrlKey) {
              e.preventDefault();
              submit();
            }
          }}
          className="block max-h-52 min-h-[2.5rem] w-full resize-none bg-transparent px-2.5 py-1.5 text-[15px] leading-6 outline-none placeholder:text-mute/50"
        />
        <div className="mt-1 flex items-center gap-1.5">
          <ModelPicker sessionId={sessionId} workspaceModel={wsData?.model ?? null} />
          <Select
            label="权限基线"
            value={level}
            onChange={setLevel}
            placement="up"
            iconOnly={phone}
            className={cn(
              danger && 'border-bad/20 bg-bad/10 text-bad hover:bg-bad/15',
              level === 'read_only' && 'border-ok/20 bg-ok/10 text-ok hover:bg-ok/15',
            )}
            leading={<LevelIcon size={13} />}
            options={LEVELS.map((l) => ({
              value: l.value,
              label: l.label,
              hint: l.hint,
              icon: <l.icon size={14} className="text-mute" />,
            }))}
          />
          <span className="min-w-0 flex-1" />
          {contextPercent !== null && (
            <ContextRing
              percent={contextPercent}
              onClick={() => {
                openCard('context');
                toggleInspector();
              }}
            />
          )}
          {busy && (
            <IconButton
              label="取消任务"
              side="top"
              onClick={onCancel}
              className="text-bad hover:text-bad"
            >
              <Square size={15} fill="currentColor" />
            </IconButton>
          )}
          <IconButton
            type="submit"
            label={busy ? '加入队列' : '发送'}
            hint="Enter"
            side="top"
            disabled={!ready}
            className={cn(
              'h-9 w-9 rounded-full',
              ready
                ? 'bg-brand text-white shadow-[0_4px_16px_-2px_rgb(var(--c-accent)/0.5)] hover:text-white hover:brightness-110'
                : 'bg-ink/10',
            )}
          >
            {busy ? <ListPlus size={17} /> : <ArrowUp size={18} strokeWidth={2.5} />}
          </IconButton>
        </div>
      </form>

      {asking !== null && (
        <Modal title="以完全权限运行？" onClose={() => setAsking(null)}>
          <p className="text-sm text-ink">
            此任务中<strong>所有操作将自动执行，不再询问</strong>
            ，包括联网、安装依赖、推送代码和系统级命令。 每次任务都要重新确认。
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button autoFocus onClick={() => setAsking(null)}>
              取消
            </Button>
            <Button variant="danger" onClick={() => void send(asking, true)}>
              确认
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
