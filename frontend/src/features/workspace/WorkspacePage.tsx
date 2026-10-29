/** 工作区概览：基本信息、工作区级设置、记忆、目录结构。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from '@tanstack/react-router';
import { Menu, Pencil, Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, isUnsupported } from '../../api/endpoints';
import { useModels } from '../../api/useSettings';
import { useWorkspaces } from '../../api/queries';
import { McpStatus } from '../../components/domain/McpStatus';
import { FolderGlyph } from '../../components/icons/Glyphs';
import { CopyButton } from '../../components/ui/CopyButton';
import { Select } from '../../components/ui/Select';
import { Tip } from '../../components/ui/Tooltip';
import { Card, IconButton } from '../../components/ui/primitives';
import { RenameWorkspaceModal } from '../../components/modals/RenameWorkspaceModal';
import { useAppStore } from '../../stores/useAppStore';
import { usePendingStore } from '../../stores/usePendingStore';
import { useUiStore } from '../../stores/useUiStore';
import type {
  PermissionLevel,
  TreeNode,
  WorkspaceMemory,
  WorkspaceSettings,
} from '../../types/api';
import { rememberLocation } from '../../utils/lastLocation';

const INHERIT = '__inherit__';
const LAYERS: { key: keyof WorkspaceMemory; label: string; color: string }[] = [
  { key: 'profile', label: '项目概况', color: '#6c91a6' },
  { key: 'guidelines', label: '规范与约定', color: '#c09a56' },
  { key: 'architecture', label: '架构', color: '#6f9a86' },
  { key: 'pitfalls', label: '避坑', color: '#b9806a' },
];

function TreeView({ node, depth = 0 }: { node: TreeNode; depth?: number }) {
  const [open, setOpen] = useState(depth < 1);
  if (!node.is_dir)
    return (
      <li
        className="flex items-center gap-1.5 py-0.5 font-mono text-[12px] text-mute"
        style={{ paddingLeft: depth * 14 + 4 }}
      >
        <span className="h-3 w-3 shrink-0" />
        <span className="truncate">{node.name}</span>
      </li>
    );
  return (
    <li>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 rounded py-0.5 text-left font-mono text-[12px] hover:bg-ink/[0.05]"
        style={{ paddingLeft: depth * 14 + 4 }}
      >
        <FolderGlyph open={open} size={13} />
        <span className="truncate">{node.name}</span>
      </button>
      {open && node.children && (
        <ul>
          {node.children.map((c) => (
            <TreeView key={c.path} node={c} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function WorkspacePage() {
  const { workspaceId = '' } = useParams({ strict: false }) as { workspaceId?: string };
  const client = useQueryClient();
  const navigate = useNavigate();
  const workspaces = useWorkspaces();
  const workspace = workspaces.data?.find((w) => w.workspace_id === workspaceId);
  const models = useModels();
  const phone = useUiStore((s) => s.phone);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const notify = useAppStore((s) => s.notify);
  const mark = usePendingStore((s) => s.mark);
  const [renaming, setRenaming] = useState(false);
  const [adding, setAdding] = useState('');
  const [layer, setLayer] = useState<keyof WorkspaceMemory>('guidelines');
  const [editingDesc, setEditingDesc] = useState<string | null>(null);

  useEffect(() => {
    if (workspaceId) rememberLocation(workspaceId);
  }, [workspaceId]);

  const settings = useQuery({
    queryKey: ['wsettings', workspaceId],
    queryFn: () => api.workspaces.settings(workspaceId),
    enabled: !!workspaceId,
    retry: false,
  });
  const memory = useQuery({
    queryKey: ['wmemory', workspaceId],
    queryFn: () => api.workspaces.memory(workspaceId),
    enabled: !!workspaceId,
  });
  const tree = useQuery({
    queryKey: ['tree', workspaceId],
    queryFn: () => api.workspaces.tree(workspaceId),
    enabled: !!workspaceId,
    retry: false,
  });

  const supported = !(settings.isError && isUnsupported(settings.error));
  const current: WorkspaceSettings = settings.data ?? {
    permission_level: null,
    model: null,
    rules_enabled: null,
  };
  const save = useMutation({
    mutationFn: (next: WorkspaceSettings) =>
      api.workspaces.saveSettings(workspaceId, {
        permission_level: next.permission_level,
        model: next.model,
        rules_enabled: next.rules_enabled,
      }),
    onSuccess: (data) => {
      client.setQueryData(['wsettings', workspaceId], data);
      mark();
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const describe = useMutation({
    mutationFn: (description: string) => api.workspaces.update(workspaceId, { description }),
    onSuccess: () => {
      setEditingDesc(null);
      return client.invalidateQueries({ queryKey: ['workspaces'] });
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const refreshMemory = () => client.invalidateQueries({ queryKey: ['wmemory', workspaceId] });
  const add = useMutation({
    mutationFn: () => api.workspaces.addFact(workspaceId, adding.trim(), layer),
    onSuccess: () => {
      setAdding('');
      return refreshMemory();
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const remove = useMutation({
    mutationFn: ({ kind, index }: { kind: keyof WorkspaceMemory; index: number }) =>
      api.workspaces.removeMemory(workspaceId, kind, index),
    onSuccess: refreshMemory,
    onError: (e: Error) => notify(e.message, 'error'),
  });

  if (!workspace)
    return (
      <div className="flex h-full items-center justify-center text-mute">
        {workspaces.isPending ? '' : '—'}
      </div>
    );

  const none = !supported ? '后端未提供' : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-ink/[0.06] px-3 dark:border-white/[0.06] sm:px-4">
        {phone && (
          <IconButton label="导航" onClick={() => setNavOpen(true)}>
            <Menu size={17} />
          </IconButton>
        )}
        <h1 className="min-w-0 truncate text-[15px] font-semibold">{workspace.name}</h1>
        <IconButton label="重命名" onClick={() => setRenaming(true)}>
          <Pencil size={14} />
        </IconButton>
        <span className="flex-1" />
        <McpStatus />
        <IconButton
          label="新会话"
          onClick={() =>
            void api.sessions
              .create(workspaceId)
              .then(async (s) => {
                await client.invalidateQueries({ queryKey: ['sessions', workspaceId] });
                void navigate({
                  to: '/w/$workspaceId/s/$sessionId',
                  params: { workspaceId, sessionId: s.session_id },
                });
              })
              .catch((e: Error) => notify(e.message, 'error'))
          }
        >
          <Plus size={17} />
        </IconButton>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto grid w-full max-w-[920px] gap-3 p-3 sm:p-5 lg:grid-cols-2">
          <Card id="ws-info" title="信息" className="lg:col-span-2">
            <div className="flex items-center gap-1 font-mono text-[12.5px] text-mute">
              <span className="min-w-0 truncate">{workspace.root_path}</span>
              <CopyButton text={workspace.root_path} label="复制路径" />
            </div>
            {editingDesc === null ? (
              <button
                className="mt-2 block w-full text-left text-[13px] text-mute hover:text-ink"
                onClick={() => setEditingDesc(workspace.description)}
              >
                {workspace.description || '描述'}
              </button>
            ) : (
              <textarea
                autoFocus
                aria-label="描述"
                rows={2}
                className="field mt-2"
                value={editingDesc}
                onChange={(e) => setEditingDesc(e.target.value)}
                onBlur={() => describe.mutate(editingDesc)}
                onKeyDown={(e) => e.key === 'Escape' && setEditingDesc(null)}
              />
            )}
          </Card>

          <Card id="ws-settings" title="设置">
            <dl className="space-y-3 text-[13px]">
              <div className="flex items-center gap-3">
                <dt className="w-24 shrink-0 text-mute">权限基线</dt>
                <dd>
                  <Select
                    label="默认权限基线"
                    disabled={!supported || save.isPending}
                    title={none}
                    value={current.permission_level ?? INHERIT}
                    onChange={(v) =>
                      save.mutate({
                        ...current,
                        permission_level: v === INHERIT ? null : (v as PermissionLevel),
                      })
                    }
                    options={[
                      { value: INHERIT, label: '工作区读写（默认）' },
                      { value: 'read_only', label: '只读' },
                      { value: 'workspace_write', label: '工作区读写' },
                      { value: 'full_permissions', label: '完全权限', hint: '每次提交仍需确认' },
                    ]}
                  />
                </dd>
              </div>
              <div className="flex items-center gap-3">
                <dt className="w-24 shrink-0 text-mute">默认模型</dt>
                <dd>
                  <Select
                    label="默认模型"
                    disabled={!supported || save.isPending}
                    title={none}
                    value={current.model ?? INHERIT}
                    onChange={(v) => save.mutate({ ...current, model: v === INHERIT ? null : v })}
                    options={[
                      { value: INHERIT, label: '跟随全局' },
                      ...(models.data ?? []).map((m) => ({
                        value: m.name,
                        label: m.name,
                        hint: m.model,
                      })),
                    ]}
                  />
                </dd>
              </div>
              <div className="flex items-center gap-3">
                <dt className="w-24 shrink-0 text-mute">项目规则</dt>
                <dd>
                  <Select
                    label="项目规则"
                    disabled={!supported || save.isPending}
                    title={none}
                    value={
                      current.rules_enabled === null
                        ? INHERIT
                        : current.rules_enabled
                          ? 'on'
                          : 'off'
                    }
                    onChange={(v) =>
                      save.mutate({ ...current, rules_enabled: v === INHERIT ? null : v === 'on' })
                    }
                    options={[
                      { value: INHERIT, label: '跟随全局' },
                      {
                        value: 'on',
                        label: '启用',
                        hint: 'CLAUDE.md / AGENTS.md 等进入每个任务的提示词',
                      },
                      { value: 'off', label: '关闭' },
                    ]}
                  />
                </dd>
              </div>
            </dl>
          </Card>

          <Card id="ws-tree" title="目录">
            {tree.data ? (
              <ul className="max-h-72 overflow-auto">
                {(tree.data.children ?? []).map((c) => (
                  <TreeView key={c.path} node={c} />
                ))}
              </ul>
            ) : (
              <p className="text-mute">{tree.isError ? (tree.error as Error).message : ''}</p>
            )}
          </Card>

          <Card id="ws-memory" title="记忆" className="lg:col-span-2">
            <div className="space-y-3">
              {LAYERS.map((l) => (
                <div key={l.key}>
                  <div className="mb-1 flex items-center gap-1.5 text-2xs font-semibold text-mute">
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: l.color }} />
                    {l.label}
                  </div>
                  <ul className="space-y-1">
                    {(memory.data?.[l.key] ?? []).map((text, index) => (
                      <li
                        key={`${index}-${text}`}
                        className="group flex items-start gap-1.5 text-[13px] leading-snug"
                      >
                        <span className="min-w-0 flex-1 break-words">{text}</span>
                        <Tip label="删除">
                          <button
                            aria-label={`删除 ${text}`}
                            className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-mute opacity-100 hover:bg-ink/[0.06] hover:text-bad sm:opacity-0 sm:focus-visible:opacity-100 sm:group-hover:opacity-100"
                            onClick={() => remove.mutate({ kind: l.key, index })}
                          >
                            <Trash2 size={12} />
                          </button>
                        </Tip>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              <form
                className="flex items-center gap-1.5 pt-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (adding.trim()) add.mutate();
                }}
              >
                <Select
                  label="记忆分类"
                  value={layer}
                  onChange={setLayer}
                  options={LAYERS.map((l) => ({ value: l.key, label: l.label }))}
                />
                <input
                  aria-label="新增记忆"
                  className="field min-w-0 flex-1 py-1.5 text-[13px]"
                  maxLength={2000}
                  value={adding}
                  onChange={(e) => setAdding(e.target.value)}
                />
                <IconButton type="submit" label="添加" disabled={!adding.trim() || add.isPending}>
                  <Plus size={16} />
                </IconButton>
              </form>
            </div>
          </Card>
        </div>
      </div>
      {renaming && (
        <RenameWorkspaceModal workspace={workspace} onClose={() => setRenaming(false)} />
      )}
    </div>
  );
}
