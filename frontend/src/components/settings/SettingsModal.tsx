/**
 * 设置面板：当前会话（模型、上下文上限）、系统提示词、外观、当前工作区。
 * 范围与缺省值都来自接口；设置在下一个任务生效。设计见 docs/10_frontend/03_visual_design.md §5。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'motion/react';
import { Check, FolderCog, MessageSquareText, Palette, ScrollText, X } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import { useModels, useRefreshContext, useSessionSettings } from '../../api/useSettings';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore, type ThemePref } from '../../stores/useUiStore';
import { cn, formatTokens } from '../../utils/text';
import { Select } from '../ui/Select';
import { Slider } from '../ui/Slider';
import { Button, IconButton, Overlay, SPRING } from '../ui/primitives';

type Section = 'session' | 'prompt' | 'appearance' | 'workspace';

const SECTIONS: { id: Section; label: string; icon: ReactNode }[] = [
  { id: 'session', label: '当前会话', icon: <MessageSquareText size={16} strokeWidth={1.7} /> },
  { id: 'prompt', label: '系统提示词', icon: <ScrollText size={16} strokeWidth={1.7} /> },
  { id: 'appearance', label: '外观', icon: <Palette size={16} strokeWidth={1.7} /> },
  { id: 'workspace', label: '当前工作区', icon: <FolderCog size={16} strokeWidth={1.7} /> },
];

const AUTO = '__auto__';

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <div>
        <div className="text-[13px] font-semibold">{label}</div>
        {hint && <div className="mt-0.5 text-xs leading-relaxed text-mute">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

function Saved({ show }: { show: boolean }) {
  return (
    <AnimatePresence>
      {show && (
        <motion.span
          initial={{ opacity: 0, x: -4 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0 }}
          className="flex items-center gap-1 text-xs font-medium text-ok"
        >
          <Check size={13} /> 已保存
        </motion.span>
      )}
    </AnimatePresence>
  );
}

function LoadError({ error }: { error: unknown }) {
  return (
    <p role="alert" className="rounded-2xl bg-bad/[0.06] px-4 py-3 text-sm text-bad">
      读取设置失败：{(error as Error).message}
    </p>
  );
}

function useFlash(): [boolean, () => void] {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (!on) return;
    const t = setTimeout(() => setOn(false), 1800);
    return () => clearTimeout(t);
  }, [on]);
  return [on, () => setOn(true)];
}

function SessionSection() {
  const sessionId = useAppStore((s) => s.sessionId);
  const models = useModels();
  const { query, save } = useSessionSettings(sessionId);
  const [model, setModel] = useState<string>(AUTO);
  const [limit, setLimit] = useState<number | null>(null);
  const [saved, flash] = useFlash();

  useEffect(() => {
    if (!query.data) return;
    setModel(query.data.model ?? AUTO);
    setLimit(query.data.context_token_limit);
  }, [query.data]);

  if (!sessionId) return <p className="text-sm text-mute">先在左侧选择一个会话。</p>;
  if (query.isError) return <LoadError error={query.error} />;
  if (!query.data) return <p className="text-sm text-mute">读取中…</p>;

  const { defaults, bounds } = query.data;
  const range = bounds.context_token_limit;
  const shown = limit ?? defaults.context_token_limit;
  const dirty =
    (model === AUTO ? null : model) !== query.data.model ||
    limit !== query.data.context_token_limit;

  return (
    <div className="space-y-7">
      <Field label="模型" hint="作为主 Agent 的首选端点；它不可用时按配置顺序自动降级。">
        <Select
          label="会话模型"
          value={model}
          onChange={setModel}
          className="px-3 py-1.5 text-[13px]"
          options={[
            { value: AUTO, label: '自动', hint: '按配置的端点顺序' },
            ...(models.data ?? []).map((m) => ({
              value: m.name,
              label: m.name,
              hint: `${m.model} · ${formatTokens(m.context_window)} 上下文`,
            })),
          ]}
        />
      </Field>

      <Field
        label="上下文上限"
        hint="活跃对话超过它时，较早的轮次会被压缩成会话记忆。右侧“上下文占比”里的对话记录会显示距这条线还有多远。"
      >
        <div className="flex items-center gap-4">
          <Slider
            label="上下文上限"
            value={shown}
            min={range.min}
            max={range.max}
            step={range.step}
            format={formatTokens}
            scale="log"
            onChange={setLimit}
          />
          <span className="w-16 shrink-0 text-right text-sm font-semibold tabular-nums">
            {formatTokens(shown)}
          </span>
        </div>
        <div className="flex items-center justify-between text-2xs text-mute">
          <span>
            {formatTokens(range.min)} – {formatTokens(range.max)}
          </span>
          <button
            className={cn('hover:text-ink', limit === null && 'invisible')}
            onClick={() => setLimit(null)}
          >
            恢复默认（{formatTokens(defaults.context_token_limit)}）
          </button>
        </div>
      </Field>

      <div className="flex items-center gap-3">
        <Button
          variant="primary"
          disabled={!dirty || save.isPending}
          onClick={() =>
            save.mutate(
              { model: model === AUTO ? null : model, context_token_limit: limit },
              { onSuccess: flash },
            )
          }
        >
          保存
        </Button>
        <Saved show={saved} />
        {save.isError && <span className="text-xs text-bad">{(save.error as Error).message}</span>}
        <span className="ml-auto text-2xs text-mute">下一个任务生效</span>
      </div>
    </div>
  );
}

function PromptSection() {
  const client = useQueryClient();
  const refreshContext = useRefreshContext();
  const settings = useQuery({ queryKey: ['appsettings'], queryFn: api.system.settings });
  const [draft, setDraft] = useState('');
  const [saved, flash] = useFlash();
  const save = useMutation({
    mutationFn: (text: string | null) => api.system.saveSettings({ system_prompt: text }),
    onSuccess: (data) => {
      client.setQueryData(['appsettings'], data);
      void refreshContext(); // 系统提示词变了，上下文占比里它的份额也要变
      flash();
    },
  });

  useEffect(() => {
    if (settings.data) setDraft(settings.data.system_prompt ?? settings.data.system_prompt_default);
  }, [settings.data]);

  if (settings.isError) return <LoadError error={settings.error} />;
  if (!settings.data) return <p className="text-sm text-mute">读取中…</p>;
  const current = settings.data.system_prompt ?? settings.data.system_prompt_default;
  const isDefault = draft.trim() === settings.data.system_prompt_default.trim();
  const customized = settings.data.system_prompt !== null;
  const over = draft.length > settings.data.system_prompt_max_chars;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-start gap-3">
        <p className="flex-1 text-xs leading-relaxed text-mute">
          替换内置提示词，作为每个任务的系统段。安全协议之外的工作方式都写在这里；工具说明与记忆渲染不受影响。
        </p>
        {customized && <span className="stamp mt-0.5 shrink-0">已自定义</span>}
      </div>
      {/* 信纸：横线随内容滚动，文字落在线上；撑满剩余高度，不留空白 */}
      <div
        className="min-h-0 flex-1 rounded-base bg-card p-3.5"
        style={{
          boxShadow: 'inset 0 1px 2px rgb(40 32 20 / 0.07), 0 0 0 1px rgb(var(--c-ink) / 0.07)',
        }}
      >
        <textarea
          aria-label="系统提示词"
          spellCheck={false}
          className="ruled block h-full w-full resize-none bg-transparent font-mono text-[12.5px] text-ink outline-none"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <Button
          variant="primary"
          disabled={draft === current || over || save.isPending}
          onClick={() => save.mutate(isDefault ? null : draft)}
        >
          保存
        </Button>
        <Button
          variant="ghost"
          disabled={isDefault}
          onClick={() => setDraft(settings.data?.system_prompt_default ?? '')}
        >
          恢复默认
        </Button>
        <Saved show={saved} />
        {save.isError && <span className="text-xs text-bad">{(save.error as Error).message}</span>}
        <span className={cn('ml-auto text-2xs tabular-nums', over ? 'text-bad' : 'text-mute')}>
          {draft.length} / {settings.data.system_prompt_max_chars}
        </span>
      </div>
    </div>
  );
}

function AppearanceSection() {
  const theme = useUiStore((s) => s.theme);
  const setTheme = useUiStore((s) => s.setTheme);
  const options: { value: ThemePref; label: string }[] = [
    { value: 'system', label: '跟随系统' },
    { value: 'light', label: '浅色' },
    { value: 'dark', label: '暗色' },
  ];
  return (
    <Field label="主题">
      <div
        role="radiogroup"
        aria-label="主题"
        className="inline-flex rounded-full bg-ink/[0.045] p-1 border border-ink/[0.06]"
      >
        {options.map((o) => (
          <button
            key={o.value}
            role="radio"
            aria-checked={theme === o.value}
            className={cn(
              'relative rounded-full px-4 py-1.5 text-[13px] font-medium transition-colors',
              theme === o.value ? 'text-ink' : 'text-mute hover:text-ink',
            )}
            onClick={() => setTheme(o.value)}
          >
            {theme === o.value && (
              <motion.span
                layoutId="theme-pill"
                transition={SPRING}
                className="absolute inset-0 rounded-full bg-card shadow-card border border-ink/[0.06] dark:border-white/[0.08]"
              />
            )}
            <span className="relative">{o.label}</span>
          </button>
        ))}
      </div>
    </Field>
  );
}

function WorkspaceSection({ onDone }: { onDone: () => void }) {
  const client = useQueryClient();
  const workspaceId = useAppStore((s) => s.workspaceId);
  const selectWorkspace = useAppStore((s) => s.selectWorkspace);
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.workspaces.list });
  const current = workspaces.data?.find((w) => w.workspace_id === workspaceId);
  const [typed, setTyped] = useState('');
  const remove = useMutation({
    mutationFn: () => api.workspaces.remove(workspaceId as string),
    onSuccess: async () => {
      selectWorkspace(null);
      await client.invalidateQueries({ queryKey: ['workspaces'] });
      onDone();
    },
  });
  if (!current) return <p className="text-sm text-mute">先在左侧选择一个工作区。</p>;
  return (
    <div className="space-y-6">
      <Field label="名称">
        <div className="text-sm">{current.name}</div>
      </Field>
      <Field label="目录">
        <code className="block break-all rounded-xl bg-ink/[0.045] px-3 py-2 font-mono text-xs border border-ink/[0.04]">
          {current.root_path}
        </code>
      </Field>
      <div className="rounded-[18px] border border-bad/30 bg-bad/[0.04] p-4.5">
        <div className="text-[13px] font-semibold text-bad">删除工作区</div>
        <p className="mt-1 text-xs leading-relaxed text-ink/80">
          这会<strong className="text-bad">级联删除</strong>
          该工作区下的全部会话、对话记录、记忆、轨迹和产物，且无法恢复。项目目录里的文件不会被删除。
        </p>
        <label className="mt-3 block text-xs">
          输入工作区名称 <code className="rounded bg-card px-1.5 font-mono">{current.name}</code>{' '}
          以确认
          <input
            aria-label="确认名称"
            className="field mt-1.5 bg-card"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
        </label>
        {remove.isError && (
          <p role="alert" className="mt-2 text-xs text-bad">
            {(remove.error as Error).message}
          </p>
        )}
        <Button
          variant="danger"
          className="mt-3"
          disabled={typed !== current.name || remove.isPending}
          onClick={() => remove.mutate()}
        >
          删除
        </Button>
      </div>
    </div>
  );
}

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [section, setSection] = useState<Section>('session');
  const title = SECTIONS.find((s) => s.id === section)?.label;
  return (
    <Overlay
      label="设置"
      onClose={onClose}
      className="h-[min(640px,88vh)] max-w-3xl"
      bodyClassName="flex"
    >
      {/* 信封内衬：防窥纹铺在导航底下，右侧是信纸 */}
      <nav
        aria-label="设置分组"
        className="tint flex w-48 shrink-0 flex-col gap-0.5 border-r border-ink/[0.07] bg-side/60 p-3"
      >
        <div className="px-2.5 pb-3 pt-1 text-[15px] font-semibold">设置</div>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            aria-current={section === s.id}
            className="nav-item"
            onClick={() => setSection(s.id)}
          >
            {s.icon}
            {s.label}
          </button>
        ))}
      </nav>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center justify-between px-7 pb-3 pt-3">
          <h2 className="text-[17px] font-semibold">{title}</h2>
          <IconButton label="关闭设置" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-7 pb-6 no-scrollbar">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={section}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.16 }}
              className="h-full"
            >
              {section === 'session' && <SessionSection />}
              {section === 'prompt' && <PromptSection />}
              {section === 'appearance' && <AppearanceSection />}
              {section === 'workspace' && <WorkspaceSection onDone={onClose} />}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </Overlay>
  );
}
