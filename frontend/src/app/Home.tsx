/** 根路径：回到最近使用的会话，没有就进第一个工作区；一个工作区都没有时显示添加入口。 */
import { Navigate } from '@tanstack/react-router';
import { FolderPlus, Menu } from 'lucide-react';
import { useState } from 'react';
import { useWorkspaces } from '../api/queries';
import { WorkspaceModal } from '../components/modals/WorkspaceModal';
import { IconButton } from '../components/ui/primitives';
import { useUiStore } from '../stores/useUiStore';
import { lastLocation } from '../utils/lastLocation';

export function Home() {
  const workspaces = useWorkspaces();
  const phone = useUiStore((s) => s.phone);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const [adding, setAdding] = useState(false);
  const list = workspaces.data ?? [];
  if (workspaces.isPending) return null;

  const last = lastLocation();
  const target = list.find((w) => w.workspace_id === last?.workspaceId) ?? list[0];
  if (target) {
    return last?.sessionId && last.workspaceId === target.workspace_id ? (
      <Navigate
        to="/w/$workspaceId/s/$sessionId"
        params={{ workspaceId: target.workspace_id, sessionId: last.sessionId }}
        replace
      />
    ) : (
      <Navigate to="/w/$workspaceId" params={{ workspaceId: target.workspace_id }} replace />
    );
  }
  return (
    <div className="flex h-full flex-col">
      {phone && (
        <header className="flex h-12 items-center px-3">
          <IconButton label="导航" onClick={() => setNavOpen(true)}>
            <Menu size={17} />
          </IconButton>
        </header>
      )}
      <div className="flex flex-1 items-center justify-center">
        <IconButton
          label="添加工作区"
          className="h-14 w-14 rounded-2xl bg-ink/[0.05]"
          onClick={() => setAdding(true)}
        >
          <FolderPlus size={24} />
        </IconButton>
      </div>
      {adding && <WorkspaceModal onClose={() => setAdding(false)} />}
    </div>
  );
}
