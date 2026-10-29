/**
 * 提示词：模板可编辑（以随发行版的初始版本为基线，可一键恢复）；动态分区只读，
 * 展示某个会话下一次请求里该段的渲染全文；安全指令固定不可编辑。
 */
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { motion } from 'motion/react';
import { ChevronDown, ChevronUp, Lock, RotateCcw, Save, Search, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, isUnsupported } from '../../../api/endpoints';
import { sessionTitle, useWorkspaces } from '../../../api/queries';
import { ConfirmDialog } from '../../../components/modals/ConfirmDialog';
import { IconButton } from '../../../components/ui/primitives';
import { Select } from '../../../components/ui/Select';
import { useAppStore } from '../../../stores/useAppStore';
import { usePendingStore } from '../../../stores/usePendingStore';
import type { PromptDetail, PromptInfo, PromptKind } from '../../../types/api';
import { cn } from '../../../utils/text';
import { Scope } from '../parts';

const TEMPLATE_ORDER = [
  'system',
  'subagent',
  'subagent_report',
  'compactor',
  'task_compactor',
  'websearch_plan',
  'websearch_extract',
];

const TITLES: Record<string, string> = {
  system: '系统提示词',
  subagent: '子智能体',
  subagent_report: '子智能体汇报',
  compactor: '长会话压缩',
  task_compactor: '任务内压缩',
  websearch_plan: '联网搜索·规划',
  websearch_extract: '联网搜索·提炼',
};

const TEMPLATE_FALLBACK: PromptInfo[] = (
  [
    ['system', '主智能体'],
    ['subagent', '子智能体'],
    ['subagent_report', '子智能体汇报'],
    ['compactor', '长会话压缩'],
    ['task_compactor', '任务内压缩'],
    ['websearch_plan', '联网搜索·规划'],
    ['websearch_extract', '联网搜索·提炼'],
  ] as const
).map(([name, usage]) => ({
  name,
  kind: 'template' as PromptKind,
  title: TITLES[name] ?? name,
  usage,
  size: 0,
  customized: false,
  max_chars: 0,
}));

const DYNAMIC: PromptInfo[] = (
  [
    ['project_rules', '项目规则'],
    ['workspace_memory', '工作区记忆'],
    ['session_memory', '会话记忆'],
    ['task_data', '环境与近期新增'],
    ['tools', '工具定义'],
    ['skills', '技能清单'],
    ['mcp_index', 'MCP 索引'],
  ] as const
).map(([name, title]) => ({
  name,
  kind: 'dynamic' as PromptKind,
  title,
  usage: '',
  size: 0,
  customized: false,
  max_chars: 0,
}));

const FIXED: PromptInfo[] = [
  {
    name: 'canary',
    kind: 'fixed',
    title: '金丝雀防御标记',
    usage: '',
    size: 0,
    customized: false,
    max_chars: 0,
  },
  {
    name: 'envelope',
    kind: 'fixed',
    title: '不可信内容信封',
    usage: '',
    size: 0,
    customized: false,
    max_chars: 0,
  },
];

function usePromptApi() {
  const list = useQuery({ queryKey: ['prompts'], queryFn: api.prompts.list, retry: false });
  const supported = !(list.isError && isUnsupported(list.error));
  const settings = useQuery({
    queryKey: ['app-settings'],
    queryFn: api.system.settings,
    enabled: !supported,
  });
  const templates = useMemo(() => {
    if (supported) {
      const listData = (list.data ?? [])
        .filter((p) => p.kind === 'template')
        .map((p) => ({ ...p, title: TITLES[p.name] ?? p.title }));
      return listData.sort((a, b) => {
        const ia = TEMPLATE_ORDER.indexOf(a.name);
        const ib = TEMPLATE_ORDER.indexOf(b.name);
        return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      });
    }
    return TEMPLATE_FALLBACK.map((p) => ({
      ...p,
      customized: p.name === 'system' && settings.data?.system_prompt != null,
    }));
  }, [supported, list.data, settings.data]);
  return { supported, templates, loading: list.isPending };
}

function Editor({ info, supported }: { info: PromptInfo; supported: boolean }) {
  const client = useQueryClient();
  const notify = useAppStore((s) => s.notify);
  const mark = usePendingStore((s) => s.mark);
  const [draft, setDraft] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);

  const detail = useQuery({
    queryKey: ['prompt', info.name, supported],
    queryFn: async (): Promise<PromptDetail> => {
      if (supported) return api.prompts.get(info.name);
      const s = await api.system.settings();
      return {
        name: 'system',
        content: s.system_prompt ?? s.system_prompt_default,
        default: s.system_prompt_default,
        customized: s.system_prompt != null,
        max_chars: s.system_prompt_max_chars,
      };
    },
  });
  useEffect(() => {
    setDraft(null);
    setShowSearch(false);
    setSearchQuery('');
  }, [info.name]);

  const d = detail.data;
  const text = draft ?? d?.content ?? '';
  const dirty = !!d && text !== d.content;
  const over = !!d && d.max_chars > 0 && text.length > d.max_chars;

  const lines = useMemo(() => text.split('\n'), [text]);
  const lineCount = lines.length;
  const charCount = text.length;
  const estTokens = Math.max(0, Math.round(charCount / 3.5));

  const matches = useMemo(() => {
    if (!searchQuery.trim()) return [];
    const list: { start: number; end: number }[] = [];
    const lowerText = text.toLowerCase();
    const q = searchQuery.toLowerCase();
    let pos = 0;
    while ((pos = lowerText.indexOf(q, pos)) !== -1) {
      list.push({ start: pos, end: pos + q.length });
      pos += q.length;
    }
    return list;
  }, [text, searchQuery]);

  const goToMatch = (index: number) => {
    if (!matches.length || !textareaRef.current) return;
    const wrapped = (index + matches.length) % matches.length;
    setActiveMatchIndex(wrapped);
    const m = matches[wrapped];
    if (!m) return;
    const el = textareaRef.current;
    el.focus();
    el.setSelectionRange(m.start, m.end);
    const linesBefore = text.slice(0, m.start).split('\n').length;
    const approxScrollTop = Math.max(0, (linesBefore - 3) * 22);
    el.scrollTop = approxScrollTop;
    if (gutterRef.current) {
      gutterRef.current.scrollTop = approxScrollTop;
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      setShowSearch((v) => !v);
    }
  };

  const done = (next: PromptDetail) => {
    client.setQueryData(['prompt', info.name, supported], next);
    void client.invalidateQueries({ queryKey: ['prompts'] });
    void client.invalidateQueries({ queryKey: ['app-settings'] });
    void client.invalidateQueries({ queryKey: ['context'] });
    setDraft(null);
    mark();
  };
  const save = useMutation({
    mutationFn: async () => {
      if (supported) return api.prompts.save(info.name, text);
      const s = await api.system.saveSettings({ system_prompt: text });
      return {
        name: 'system',
        content: s.system_prompt ?? s.system_prompt_default,
        default: s.system_prompt_default,
        customized: s.system_prompt != null,
        max_chars: s.system_prompt_max_chars,
      };
    },
    onSuccess: done,
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const reset = useMutation({
    mutationFn: async () => {
      if (supported) return api.prompts.reset(info.name);
      const s = await api.system.saveSettings({ system_prompt: null });
      return {
        name: 'system',
        content: s.system_prompt_default,
        default: s.system_prompt_default,
        customized: false,
        max_chars: s.system_prompt_max_chars,
      };
    },
    onSuccess: done,
    onError: (e: Error) => notify(e.message, 'error'),
  });

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="mb-2.5 flex shrink-0 flex-wrap items-center gap-2">
        <h2 className="text-[15px] font-semibold">{info.title}</h2>
        <Scope>全局</Scope>
        {d?.customized && <span className="stamp">已自定义</span>}
        <span className="flex-1" />
        <IconButton
          label="搜索 (Ctrl+F)"
          active={showSearch}
          onClick={() => setShowSearch((v) => !v)}
        >
          <Search size={15} />
        </IconButton>
        <IconButton
          label="恢复初始"
          disabled={!d?.customized && !dirty}
          onClick={() => setResetting(true)}
        >
          <RotateCcw size={15} />
        </IconButton>
        <IconButton
          label="保存"
          disabled={!dirty || over || save.isPending || !text.trim()}
          className={cn(
            dirty && !over && 'bg-brand text-white hover:text-white hover:brightness-110',
          )}
          onClick={() => save.mutate()}
        >
          <Save size={15} />
        </IconButton>
      </div>

      <div
        className="relative flex flex-1 min-h-0 flex-col overflow-hidden rounded-xl border border-line bg-card shadow-sm transition-all focus-within:border-accent/40 focus-within:ring-2 focus-within:ring-accent/10"
        onKeyDown={handleKeyDown}
      >
        {showSearch && (
          <div className="flex shrink-0 items-center gap-2 border-b border-line/40 bg-ink/[0.02] px-3 py-1.5 text-xs">
            <Search size={13} className="text-mute" />
            <input
              type="search"
              autoFocus
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                setActiveMatchIndex(0);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  if (e.shiftKey) {
                    goToMatch(activeMatchIndex - 1);
                  } else {
                    goToMatch(activeMatchIndex + 1);
                  }
                } else if (e.key === 'Escape') {
                  setShowSearch(false);
                }
              }}
              placeholder="在提示词中查找... (Enter 下一个 / Shift+Enter 上一个)"
              className="field h-7 flex-1 rounded-lg px-2 text-xs"
            />
            <span className="tabular-nums text-mute text-2xs">
              {matches.length > 0
                ? `${activeMatchIndex + 1}/${matches.length}`
                : searchQuery
                  ? '无匹配'
                  : ''}
            </span>
            <IconButton
              label="上一个匹配"
              disabled={matches.length === 0}
              onClick={() => goToMatch(activeMatchIndex - 1)}
              className="h-6 w-6"
            >
              <ChevronUp size={13} />
            </IconButton>
            <IconButton
              label="下一个匹配"
              disabled={matches.length === 0}
              onClick={() => goToMatch(activeMatchIndex + 1)}
              className="h-6 w-6"
            >
              <ChevronDown size={13} />
            </IconButton>
            <IconButton
              label="关闭搜索"
              onClick={() => setShowSearch(false)}
              className="h-6 w-6"
            >
              <X size={13} />
            </IconButton>
          </div>
        )}
        <div className="relative flex flex-1 min-h-0 overflow-hidden">
          <div
            ref={gutterRef}
            className="select-none overflow-hidden border-r border-line/40 bg-ink/[0.02] py-2.5 pl-3 pr-2 text-right font-mono text-[12.5px] leading-relaxed text-mute/40 tabular-nums shrink-0"
            aria-hidden="true"
          >
            {Array.from({ length: lineCount }, (_, i) => (
              <div key={i + 1}>{i + 1}</div>
            ))}
          </div>
          <textarea
            ref={textareaRef}
            aria-label={info.title}
            spellCheck={false}
            value={text}
            onChange={(e) => setDraft(e.target.value)}
            onScroll={(e) => {
              if (gutterRef.current) {
                gutterRef.current.scrollTop = e.currentTarget.scrollTop;
              }
            }}
            className="h-full w-full flex-1 resize-none overflow-auto whitespace-pre bg-transparent px-3 py-2.5 font-mono text-[12.5px] leading-relaxed text-ink outline-none"
          />
        </div>
      </div>

      <div
        className={cn(
          'mt-2 flex shrink-0 items-center justify-between text-2xs tabular-nums',
          over ? 'text-bad' : 'text-mute',
        )}
      >
        <div className="flex items-center gap-3">
          <span>{lineCount.toLocaleString()} 行</span>
          <span>{charCount.toLocaleString()} 字符</span>
          <span>约 {estTokens.toLocaleString()} tokens</span>
        </div>
        <div>
          {charCount.toLocaleString()}
          {d && d.max_chars > 0 ? ` / ${d.max_chars.toLocaleString()}` : ''}
        </div>
      </div>

      {resetting && (
        <ConfirmDialog
          title="恢复初始版本"
          confirmLabel="恢复"
          onClose={() => setResetting(false)}
          onConfirm={() => reset.mutateAsync()}
        >
          当前内容将被随发行版的初始版本替换。
        </ConfirmDialog>
      )}
    </div>
  );
}

function SessionPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const workspaces = useWorkspaces();
  const lists = useQueries({
    queries: (workspaces.data ?? []).map((w) => ({
      queryKey: ['sessions', w.workspace_id],
      queryFn: () => api.sessions.list(w.workspace_id),
    })),
  });
  const options = (workspaces.data ?? []).flatMap((w, i) =>
    (lists[i]?.data ?? []).map((s) => ({
      value: s.session_id,
      label: `${w.name} · ${sessionTitle(s)}`,
    })),
  );
  useEffect(() => {
    if (!value && options[0]) onChange(options[0].value);
  }, [value, options, onChange]);
  return (
    <Select
      label="会话"
      value={value}
      onChange={onChange}
      options={options.length ? options : [{ value: '', label: '—' }]}
    />
  );
}

function DynamicViewer({ info }: { info: PromptInfo }) {
  const [sessionId, setSessionId] = useState('');
  const q = useQuery({
    queryKey: ['segment', sessionId, info.name],
    queryFn: () => api.sessions.contextSegment(sessionId, info.name),
    enabled: !!sessionId,
    retry: false,
  });
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="mb-2.5 flex shrink-0 flex-wrap items-center gap-2">
        <h2 className="text-[15px] font-semibold">{info.title}</h2>
        <Lock size={13} className="text-mute" aria-label="只读" />
        <span className="flex-1" />
        <SessionPicker value={sessionId} onChange={setSessionId} />
      </div>
      {q.isError ? (
        <p className="text-sm text-mute">
          {isUnsupported(q.error) ? '后端未提供' : (q.error as Error).message}
        </p>
      ) : (
        <pre className="flex-1 min-h-0 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-line bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
          {q.data?.text ?? ''}
        </pre>
      )}
    </div>
  );
}

const FIXED_TEXT: Record<string, string> = {
  canary:
    '每个任务带一枚随机标记，一旦出现在思考、回答或工具参数里就立即终止任务。固定，不可编辑。',
  envelope:
    '来自工具、网页、MCP 的内容都被包进“不可信内容”信封，并声明其中的指令不得执行。固定，不可编辑。',
};

function UnsupportedTemplate({ info }: { info: PromptInfo }) {
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="mb-2.5 flex shrink-0 flex-wrap items-center gap-2">
        <h2 className="text-[15px] font-semibold">{info.title}</h2>
        <Scope>全局</Scope>
      </div>
      <p className="max-w-prose text-[13.5px] leading-relaxed text-mute">
        后端未提供模板内容接口。当前只有系统提示词可查看与编辑；其余模板仍在
        <code className="mx-1 rounded bg-ink/[0.06] px-1 font-mono text-[12px]">
          backend/src/agent_runtime/prompts/
        </code>
        下，可直接改文件。
      </p>
    </div>
  );
}

export function Prompts() {
  const { supported, templates } = usePromptApi();
  const all = useMemo(() => [...templates, ...DYNAMIC, ...FIXED], [templates]);
  const [selected, setSelected] = useState('system');
  const info = all.find((p) => p.name === selected) ?? all[0];

  const groups: { title: string; items: PromptInfo[] }[] = [
    { title: '智能体核心模板', items: templates },
    { title: '实时动态上下文', items: DYNAMIC },
    { title: '安全合规防御', items: FIXED },
  ];

  return (
    <div className="grid h-full min-h-0 flex-1 gap-4 md:grid-cols-[14rem_minmax(0,1fr)] lg:gap-6">
      <nav aria-label="提示词分区" className="flex h-full min-h-0 flex-col pr-1 md:overflow-y-auto">
        <div className="mb-3 shrink-0 md:hidden">
          <Select
            label="分区"
            value={info?.name ?? ''}
            onChange={setSelected}
            options={all.map((p) => ({ value: p.name, label: p.title }))}
          />
        </div>
        <div className="hidden space-y-4 md:block">
          {groups.map((g) => (
            <div key={g.title}>
              <div className="eyebrow mb-1.5 px-2 text-2xs font-semibold tracking-wider text-mute/80 uppercase">
                {g.title}
              </div>
              <ul className="space-y-0.5">
                {g.items.map((p) => {
                  const active = p.name === info?.name;
                  return (
                    <li key={p.name}>
                      <button
                        onClick={() => setSelected(p.name)}
                        aria-current={active ? 'true' : undefined}
                        className={cn(
                          'group flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition-colors',
                          active ? 'font-medium text-ink' : 'text-mute hover:text-ink',
                        )}
                      >
                        <span className="relative inline-block min-w-0 max-w-full">
                          <span className="block truncate">{p.title}</span>
                          {active && (
                            <motion.div
                              layoutId="active-prompt-category-underline"
                              className="absolute -bottom-0.5 inset-x-0 h-[2px] rounded-full bg-accent"
                              transition={{ type: 'spring', stiffness: 450, damping: 35 }}
                            />
                          )}
                        </span>
                        <span className="min-w-0 flex-1" />
                        {p.customized && (
                          <span
                            className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
                            aria-label="已自定义"
                            title="已自定义"
                          />
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      </nav>
      <div className="flex h-full min-h-0 flex-1 flex-col">
        {info?.kind === 'template' &&
          (supported || info.name === 'system' ? (
            <Editor key={info.name} info={info} supported={supported} />
          ) : (
            <UnsupportedTemplate key={info.name} info={info} />
          ))}
        {info?.kind === 'dynamic' && <DynamicViewer key={info.name} info={info} />}
        {info?.kind === 'fixed' && (
          <div className="flex h-full min-h-0 flex-1 flex-col">
            <div className="mb-2.5 flex shrink-0 items-center gap-2">
              <h2 className="text-[15px] font-semibold">{info.title}</h2>
              <Lock size={13} className="text-mute" aria-label="固定" />
            </div>
            <div className="rounded-xl border border-line bg-card p-4">
              <p className="text-[13.5px] leading-relaxed text-mute">{FIXED_TEXT[info.name]}</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
