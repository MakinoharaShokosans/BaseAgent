/**
 * 提示词：模板可编辑（以随发行版的初始版本为基线，可一键恢复）；动态分区只读，
 * 展示某个会话下一次请求里该段的渲染全文；安全指令固定不可编辑。
 */
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Lock, RotateCcw, Save } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api, isUnsupported } from '../../../api/endpoints';
import { sessionTitle, useWorkspaces } from '../../../api/queries';
import { ConfirmDialog } from '../../../components/modals/ConfirmDialog';
import { IconButton } from '../../../components/ui/primitives';
import { Select } from '../../../components/ui/Select';
import { Segmented } from '../../../components/ui/Toggle';
import { useAppStore } from '../../../stores/useAppStore';
import { usePendingStore } from '../../../stores/usePendingStore';
import type { PromptDetail, PromptInfo, PromptKind } from '../../../types/api';
import { diffLines } from '../../../utils/diff';
import { cn } from '../../../utils/text';
import { Scope } from '../parts';

const TITLES: Record<string, string> = {
  system: '系统提示词',
  compactor: '会话压缩',
  task_compactor: '任务内压缩',
  subagent: '子智能体',
  subagent_report: '子智能体汇报',
  websearch_plan: '联网搜索·规划',
  websearch_extract: '联网搜索·提炼',
};

const TEMPLATE_FALLBACK: PromptInfo[] = (
  [
    ['system', '主 Agent'],
    ['compactor', '会话压缩'],
    ['task_compactor', '任务内压缩'],
    ['subagent', '子智能体'],
    ['subagent_report', '子智能体汇报'],
    ['websearch_plan', '联网搜索·规划'],
    ['websearch_extract', '联网搜索·提炼'],
  ] as [string, string][]
).map(([name, usage]) => ({
  name,
  kind: 'template' as PromptKind,
  title: TITLES[name] ?? name,
  usage,
  size: 0,
  customized: false,
  max_chars: 0,
}));

const DYNAMIC: PromptInfo[] = [
  ['tools', '工具定义'],
  ['skills', '技能清单'],
  ['mcp_index', 'MCP 索引'],
  ['project_rules', '项目规则'],
  ['workspace_memory', '工作区记忆'],
  ['session_memory', '会话记忆'],
  ['task_data', '环境与近期新增'],
].map(([name, title]) => ({
  name: name as string,
  kind: 'dynamic' as PromptKind,
  title: title as string,
  usage: '',
  size: 0,
  customized: false,
  max_chars: 0,
}));

const FIXED: PromptInfo[] = [
  {
    name: 'canary',
    kind: 'fixed',
    title: '安全标记',
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
    if (supported)
      return (list.data ?? [])
        .filter((p) => p.kind === 'template')
        .map((p) => ({ ...p, title: TITLES[p.name] ?? p.title }));
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
  const [mode, setMode] = useState<'edit' | 'diff'>('edit');
  const [resetting, setResetting] = useState(false);

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
  useEffect(() => setDraft(null), [info.name]);
  const d = detail.data;
  const text = draft ?? d?.content ?? '';
  const dirty = !!d && text !== d.content;
  const over = !!d && d.max_chars > 0 && text.length > d.max_chars;

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
  });

  const diff = useMemo(
    () => (mode === 'diff' && d ? diffLines(d.default, text) : []),
    [mode, d, text],
  );

  return (
    <div className="flex min-h-0 flex-col">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-[15px] font-semibold">{info.title}</h2>
        <Scope>全局</Scope>
        {d?.customized && <span className="stamp">已自定义</span>}
        <span className="flex-1" />
        <Segmented
          label="视图"
          value={mode}
          onChange={setMode}
          options={[
            { value: 'edit', label: '编辑' },
            { value: 'diff', label: '对比初始' },
          ]}
        />
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
      {mode === 'edit' ? (
        <textarea
          aria-label={info.title}
          spellCheck={false}
          value={text}
          onChange={(e) => setDraft(e.target.value)}
          className="field min-h-[22rem] flex-1 resize-y font-mono text-[12.5px] leading-relaxed"
        />
      ) : (
        <div className="max-h-[34rem] min-h-[22rem] overflow-auto rounded-xl bg-code py-2 font-mono text-[12px] leading-[1.6] text-code-ink">
          {diff.map((l, i) => (
            <div
              key={i}
              className={cn(
                'flex whitespace-pre-wrap break-all px-3',
                l.kind === 'add' && 'bg-ok/15',
                l.kind === 'del' && 'bg-bad/15',
              )}
            >
              <span
                className={cn(
                  'w-4 shrink-0 select-none',
                  l.kind === 'add' ? 'text-ok' : l.kind === 'del' ? 'text-bad' : 'text-code-ink/30',
                )}
              >
                {l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : ' '}
              </span>
              <span className="min-w-0 flex-1">{l.text || ' '}</span>
            </div>
          ))}
        </div>
      )}
      <div
        className={cn('mt-1.5 text-right text-2xs tabular-nums', over ? 'text-bad' : 'text-mute')}
      >
        {text.length.toLocaleString()}
        {d && d.max_chars > 0 ? ` / ${d.max_chars.toLocaleString()}` : ''}
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
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
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
        <pre className="max-h-[34rem] min-h-[10rem] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
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
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
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
    { title: '模板', items: templates },
    { title: '动态分区', items: DYNAMIC },
    { title: '固定', items: FIXED },
  ];

  return (
    <div className="grid gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
      <nav aria-label="提示词分区" className="md:space-y-3">
        <div className="md:hidden">
          <Select
            label="分区"
            value={info?.name ?? ''}
            onChange={setSelected}
            options={all.map((p) => ({ value: p.name, label: p.title }))}
          />
        </div>
        <div className="hidden space-y-3 md:block">
          {groups.map((g) => (
            <div key={g.title}>
              <div className="eyebrow mb-1 px-2">{g.title}</div>
              <ul className="space-y-0.5">
                {g.items.map((p) => (
                  <li key={p.name}>
                    <button
                      onClick={() => setSelected(p.name)}
                      aria-current={p.name === info?.name ? 'true' : undefined}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] transition-colors',
                        p.name === info?.name
                          ? 'bg-ink/[0.07] font-medium'
                          : 'text-mute hover:bg-ink/[0.045] hover:text-ink',
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{p.title}</span>
                      {p.customized && (
                        <span
                          className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
                          aria-label="已自定义"
                        />
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </nav>
      <div className="min-w-0">
        {info?.kind === 'template' &&
          (supported || info.name === 'system' ? (
            <Editor key={info.name} info={info} supported={supported} />
          ) : (
            <UnsupportedTemplate key={info.name} info={info} />
          ))}
        {info?.kind === 'dynamic' && <DynamicViewer key={info.name} info={info} />}
        {info?.kind === 'fixed' && (
          <div>
            <div className="mb-2 flex items-center gap-2">
              <h2 className="text-[15px] font-semibold">{info.title}</h2>
              <Lock size={13} className="text-mute" aria-label="固定" />
            </div>
            <p className="text-[13.5px] leading-relaxed text-mute">{FIXED_TEXT[info.name]}</p>
          </div>
        )}
      </div>
    </div>
  );
}
