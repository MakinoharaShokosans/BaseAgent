import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import type { Workspace } from '../../types/api';
import { Button, Modal } from '../ui/primitives';

export function RenameWorkspaceModal({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(workspace.name);
  const trimmed = name.trim();

  const rename = useMutation({
    mutationFn: () => api.workspaces.update(workspace.workspace_id, { name: trimmed }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['workspaces'] });
      onClose();
    },
  });

  const canSave = !!trimmed && trimmed !== workspace.name && !rename.isPending;

  return (
    <Modal title="重命名工作区" onClose={onClose}>
      <form
        className="space-y-4 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) rename.mutate();
        }}
      >
        <label className="block">
          <span className="text-mute">名称</span>
          <input
            aria-label="名称"
            className="field mt-1"
            autoFocus
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <p className="text-xs text-mute">只改显示名，不影响磁盘上的目录。</p>
        {rename.isError && (
          <p role="alert" className="text-bad">
            {(rename.error as Error).message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button type="submit" variant="primary" disabled={!canSave}>
            保存
          </Button>
        </div>
      </form>
    </Modal>
  );
}
