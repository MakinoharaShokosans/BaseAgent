import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Eye, ShieldAlert, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../../api/endpoints';
import { SafeMarkdown } from '../../../components/common/SafeMarkdown';
import { IconButton, Modal } from '../../../components/ui/primitives';
import { Toggle } from '../../../components/ui/Toggle';
import { Tip } from '../../../components/ui/Tooltip';
import { useAppStore } from '../../../stores/useAppStore';
import { usePendingStore } from '../../../stores/usePendingStore';
import type { SkillInfo, SkillSource } from '../../../types/api';
import { ConfirmDialog } from '../../../components/modals/ConfirmDialog';
import { Group, Row, Scope, UNSUPPORTED, supportedBy } from '../parts';

const SOURCE_LABEL: Record<SkillInfo['source'], string> = {
  builtin: '内置',
  global: '全局',
  workspace: '工作区',
};

function SkillBody({
  name,
  workspaceId,
  onClose,
}: {
  name: string;
  workspaceId?: string;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ['skill-body', name, workspaceId],
    queryFn: () => api.skills.body(name, workspaceId),
    retry: false,
  });
  return (
    <Modal title={name} onClose={onClose} wide>
      {q.isError && (
        <p className="text-sm text-mute">
          {supportedBy(q) ? (q.error as Error).message : UNSUPPORTED}
        </p>
      )}
      {q.data && <SafeMarkdown text={q.data.content} />}
    </Modal>
  );
}

export function Skills() {
  const client = useQueryClient();
  const notify = useAppStore((s) => s.notify);
  const mark = usePendingStore((s) => s.mark);
  const skills = useQuery({ queryKey: ['skills'], queryFn: api.skills.list });
  const sources = useQuery({
    queryKey: ['skill-sources'],
    queryFn: api.skills.sources,
    retry: false,
  });
  const [viewing, setViewing] = useState<{ name: string; workspaceId?: string } | null>(null);
  const [trusting, setTrusting] = useState(false);

  const canToggleSources = supportedBy(sources);
  // 技能开关是否可用：列表里有 enabled 字段才说明后端支持
  const canToggle = (skills.data ?? []).some((s) => s.enabled !== undefined);

  const toggleSkill = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.skills.setEnabled(name, enabled),
    onSuccess: () => {
      mark();
      return client.invalidateQueries({ queryKey: ['skills'] });
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });
  const toggleSource = useMutation({
    mutationFn: ({ source, enabled }: { source: string; enabled: boolean }) =>
      api.skills.setSource(source, enabled),
    onSuccess: () => {
      mark();
      void client.invalidateQueries({ queryKey: ['skills'] });
      return client.invalidateQueries({ queryKey: ['skill-sources'] });
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const bySource = (s: SkillInfo['source']) => (skills.data ?? []).filter((k) => k.source === s);
  const sourceOf = (s: SkillInfo['source']): SkillSource | undefined =>
    sources.data?.find((x) => x.source === s);

  return (
    <>
      {(['builtin', 'global', 'workspace'] as const).map((src) => {
        const list = bySource(src);
        const info = sourceOf(src);
        const untrusted = src === 'workspace';
        return (
          <Group
            key={src}
            title={`${SOURCE_LABEL[src]} · ${list.length}`}
            aside={
              <span className="flex items-center gap-2">
                {untrusted && (
                  <Tip label="不可信来源：来自被指向的仓库，默认关闭">
                    <ShieldAlert size={14} className="text-warn" aria-label="不可信来源" />
                  </Tip>
                )}
                <Toggle
                  label={`${SOURCE_LABEL[src]}来源`}
                  checked={info?.enabled ?? src !== 'workspace'}
                  disabled={!canToggleSources || toggleSource.isPending}
                  tip={canToggleSources ? `${SOURCE_LABEL[src]}来源` : UNSUPPORTED}
                  onChange={(enabled) =>
                    untrusted && enabled
                      ? setTrusting(true)
                      : toggleSource.mutate({ source: src, enabled })
                  }
                />
              </span>
            }
          >
            {list.length === 0 && <div className="py-3 text-[13px] text-mute">—</div>}
            {list.map((k) => (
              <Row
                key={`${k.source}-${k.workspace_id ?? ''}-${k.name}`}
                title={
                  <span className="flex items-center gap-2">
                    <span className="font-medium">{k.name}</span>
                    {k.high_privilege.length > 0 && (
                      <Tip label={`需要高权限工具：${k.high_privilege.join('、')}`}>
                        <ShieldAlert size={13} className="text-warn" />
                      </Tip>
                    )}
                    {k.warnings.length > 0 && (
                      <Tip label={k.warnings.join('；')}>
                        <TriangleAlert size={13} className="text-warn" />
                      </Tip>
                    )}
                  </span>
                }
                note={
                  <>
                    {k.description}
                    {k.workspace_name && <Scope>{k.workspace_name}</Scope>}
                    {k.triggers.length > 0 && (
                      <span className="mt-1 flex flex-wrap gap-1">
                        {k.triggers.map((t) => (
                          <Scope key={t}>{t}</Scope>
                        ))}
                      </span>
                    )}
                  </>
                }
              >
                <IconButton
                  label="查看"
                  onClick={() => setViewing({ name: k.name, workspaceId: k.workspace_id })}
                >
                  <Eye size={15} />
                </IconButton>
                <Toggle
                  label={`启用 ${k.name}`}
                  checked={k.enabled ?? true}
                  disabled={!canToggle || toggleSkill.isPending}
                  tip={canToggle ? undefined : UNSUPPORTED}
                  onChange={(enabled) => toggleSkill.mutate({ name: k.name, enabled })}
                />
              </Row>
            ))}
          </Group>
        );
      })}
      {skills.isError && <p className="py-3 text-sm text-bad">{(skills.error as Error).message}</p>}
      {viewing && (
        <SkillBody
          name={viewing.name}
          workspaceId={viewing.workspaceId}
          onClose={() => setViewing(null)}
        />
      )}
      {trusting && (
        <ConfirmDialog
          title="启用工作区技能"
          confirmLabel="启用"
          onClose={() => setTrusting(false)}
          onConfirm={() => toggleSource.mutateAsync({ source: 'workspace', enabled: true })}
        >
          工作区技能来自被指向的仓库，克隆一个恶意仓库即可让它进入提示词。启用等于信任这些仓库的内容。
        </ConfirmDialog>
      )}
    </>
  );
}
