import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { useNavigate } from '@tanstack/react-router';
import { DirectoryPicker } from '../common/DirectoryPicker';
import { Button, Modal } from '../ui/primitives';

export function WorkspaceModal({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [rootPath, setRootPath] = useState('');
  const [browsing, setBrowsing] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.workspaces.create({ name: name.trim() || basename(rootPath), root_path: rootPath }),
    onSuccess: async (workspace) => {
      await client.invalidateQueries({ queryKey: ['workspaces'] });
      void navigate({ to: '/w/$workspaceId', params: { workspaceId: workspace.workspace_id } });
      onClose();
    },
  });

  return (
    <Modal title="添加工作区" onClose={onClose} wide>
      <div className="space-y-4 text-sm">
        <label className="block">
          <span className="text-mute">项目目录（绝对路径）</span>
          <div className="mt-1 flex gap-2">
            <input
              aria-label="项目目录"
              className="field flex-1 font-mono text-xs"
              placeholder="/home/you/Projects/my-app"
              value={rootPath}
              onChange={(e) => setRootPath(e.target.value)}
            />
            <Button onClick={() => setBrowsing((v) => !v)}>浏览…</Button>
          </div>
        </label>
        {browsing && (
          <DirectoryPicker
            onPick={(path) => {
              setRootPath(path);
              setBrowsing(false);
            }}
          />
        )}
        <label className="block">
          <span className="text-mute">名称</span>
          <input
            aria-label="名称"
            className="field mt-1"
            placeholder={rootPath ? basename(rootPath) : '缺省取目录名'}
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <p className="rounded-xl bg-accent-2/[0.07] border border-accent-2/15 px-3 py-2 text-xs text-mute leading-relaxed">
          工作区目录是安全边界：文件接口与工具只能在其中读写。根目录、系统目录和你的主目录本身会被拒绝。
        </p>
        {create.isError && (
          <p role="alert" className="text-bad">
            {(create.error as Error).message}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            disabled={!rootPath.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            添加
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function basename(path: string): string {
  return path.replace(/\/+$/, '').split('/').pop() || 'workspace';
}
