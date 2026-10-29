/** 工作区详情：一行一行展示基本信息与偏好设置，无多余折叠，纯净极简风格。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from '@tanstack/react-router';
import {
  AlignLeft,
  BookOpen,
  Cpu,
  Folder,
  Menu,
  Pencil,
  Plus,
  Shield,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, isUnsupported } from '../../api/endpoints';
import { useModels } from '../../api/useSettings';
import { useWorkspaces } from '../../api/queries';
import { McpStatus } from '../../components/domain/McpStatus';
import { CopyButton } from '../../components/ui/CopyButton';
import { Select } from '../../components/ui/Select';
import { IconButton } from '../../components/ui/primitives';
import { RenameWorkspaceModal } from '../../components/modals/RenameWorkspaceModal';
import { useAppStore } from '../../stores/useAppStore';
import { usePendingStore } from '../../stores/usePendingStore';
import { useUiStore } from '../../stores/useUiStore';
import type { PermissionLevel, WorkspaceSettings } from '../../types/api';
import { rememberLocation } from '../../utils/lastLocation';

const INHERIT = '__inherit__';

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
  const [editingDesc, setEditingDesc] = useState<string | null>(null);
  const [descDraft, setDescDraft] = useState('');

  useEffect(() => {
    if (workspaceId) rememberLocation(workspaceId);
  }, [workspaceId]);

  const settings = useQuery({
    queryKey: ['wsettings', workspaceId],
    queryFn: () => api.workspaces.settings(workspaceId),
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
        <div className="mx-auto w-full max-w-[800px] p-4 sm:p-6 lg:p-8">
          <div className="overflow-hidden rounded-2xl border border-ink/[0.07] bg-card shadow-sm divide-y divide-ink/[0.06] dark:border-white/[0.08] dark:divide-white/[0.07]">
            {/* 路径 */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
              <div className="flex items-center gap-3 min-w-0">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] text-mute dark:bg-white/[0.04]">
                  <Folder size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-[13.5px] font-medium text-ink">路径</div>
                  <div className="text-2xs text-mute">工作区在本地文件系统的绝对路径</div>
                </div>
              </div>
              <div className="flex items-center gap-2 font-mono text-[12.5px] text-mute pl-11 sm:pl-0 shrink-0">
                <span className="truncate max-w-[320px] sm:max-w-[420px] select-all">
                  {workspace.root_path}
                </span>
                <CopyButton text={workspace.root_path} label="复制路径" />
              </div>
            </div>

            {/* 描述 */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
              <div className="flex items-center gap-3 min-w-0">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] text-mute dark:bg-white/[0.04]">
                  <AlignLeft size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-[13.5px] font-medium text-ink">描述</div>
                  <div className="text-2xs text-mute">用于记录该工作区的项目定位与说明</div>
                </div>
              </div>
              <div className="pl-11 sm:pl-0 shrink-0">
                {editingDesc === null ? (
                  <button
                    type="button"
                    className="group flex items-center gap-1.5 text-left text-[13px] text-mute hover:text-ink transition-colors"
                    onClick={() => {
                      setDescDraft(workspace.description || '');
                      setEditingDesc(workspace.description || '');
                    }}
                  >
                    <span className="truncate max-w-[280px] sm:max-w-[360px]">
                      {workspace.description || '点击添加描述…'}
                    </span>
                    <Pencil
                      size={12}
                      className="opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                    />
                  </button>
                ) : (
                  <div className="flex items-center gap-2 w-full max-w-sm">
                    <input
                      autoFocus
                      aria-label="工作区描述"
                      className="field h-8 flex-1 text-[13px]"
                      placeholder="输入工作区描述…"
                      value={descDraft}
                      onChange={(e) => setDescDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') describe.mutate(descDraft);
                        if (e.key === 'Escape') setEditingDesc(null);
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => describe.mutate(descDraft)}
                      disabled={describe.isPending}
                      className="shrink-0 rounded-lg bg-ink/[0.08] px-2.5 py-1 text-xs font-medium text-ink hover:bg-ink/[0.14] transition-colors"
                    >
                      保存
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingDesc(null)}
                      className="shrink-0 rounded-lg px-2 py-1 text-xs text-mute hover:text-ink transition-colors"
                    >
                      取消
                    </button>
                  </div>
                )}
              </div>
            </div>

            {/* 权限基线 */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
              <div className="flex items-center gap-3 min-w-0">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] text-mute dark:bg-white/[0.04]">
                  <Shield size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-[13.5px] font-medium text-ink">权限基线</div>
                  <div className="text-2xs text-mute">控制主 Agent 在该工作区内的默认操作权限</div>
                </div>
              </div>
              <div className="pl-11 sm:pl-0 shrink-0">
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
              </div>
            </div>

            {/* 默认模型 */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
              <div className="flex items-center gap-3 min-w-0">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] text-mute dark:bg-white/[0.04]">
                  <Cpu size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-[13.5px] font-medium text-ink">默认模型</div>
                  <div className="text-2xs text-mute">新建会话时优先使用的推理模型</div>
                </div>
              </div>
              <div className="pl-11 sm:pl-0 shrink-0">
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
              </div>
            </div>

            {/* 项目规则 */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
              <div className="flex items-center gap-3 min-w-0">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ink/[0.04] text-mute dark:bg-white/[0.04]">
                  <BookOpen size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-[13.5px] font-medium text-ink">项目规则</div>
                  <div className="text-2xs text-mute">自动加载根目录下的 AGENTS.md / RULES.md 规则文件</div>
                </div>
              </div>
              <div className="pl-11 sm:pl-0 shrink-0">
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
              </div>
            </div>
          </div>
        </div>
      </div>
      {renaming && (
        <RenameWorkspaceModal workspace={workspace} onClose={() => setRenaming(false)} />
      )}
    </div>
  );
}
