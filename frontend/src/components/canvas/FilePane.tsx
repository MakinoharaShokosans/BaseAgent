import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { ApiError } from '../../api/http';
import { api } from '../../api/endpoints';
import { useAppStore } from '../../stores/useAppStore';
import type { TreeNode } from '../../types/api';
import { stripControl } from '../../utils/text';
import { FileGlyph, FolderGlyph } from '../icons/Glyphs';
import { Button, Chevron } from '../ui/primitives';
import { cn } from '../../utils/text';

function Tree({
  node,
  depth,
  onOpen,
  selected,
}: {
  node: TreeNode;
  depth: number;
  onOpen: (path: string) => void;
  selected: string | null;
}) {
  const [open, setOpen] = useState(depth < 1);
  return (
    <li>
      <button
        className={cn(
          'flex w-full items-center gap-1.5 truncate rounded-md py-[3px] pr-2 text-left text-[12.5px] transition-colors hover:bg-ink/[0.05]',
          selected === node.path && 'bg-accent/10 font-semibold text-ink',
        )}
        style={{ paddingLeft: depth * 12 + 6 }}
        title={node.path}
        onClick={() => (node.is_dir ? setOpen((v) => !v) : onOpen(node.path))}
      >
        <span aria-hidden className="flex w-3 justify-center">
          {node.is_dir && <Chevron open={open} size={11} />}
        </span>
        {node.is_dir ? (
          <FolderGlyph open={open} size={14} />
        ) : (
          <FileGlyph name={node.name} size={14} />
        )}
        <span className="truncate">{node.name}</span>
      </button>
      {node.is_dir && open && node.children && (
        <ul>
          {node.children.map((c) => (
            <Tree key={c.path} node={c} depth={depth + 1} onOpen={onOpen} selected={selected} />
          ))}
        </ul>
      )}
    </li>
  );
}

/** 文件查看与编辑。保存带 base_hash 冲突检测：Agent 同时改了同一文件时不会被静默覆盖。 */
export function FilePane() {
  const workspaceId = useAppStore((s) => s.workspaceId);
  const client = useQueryClient();
  const [path, setPath] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);

  const tree = useQuery({
    queryKey: ['tree', workspaceId],
    queryFn: () => api.workspaces.tree(workspaceId as string),
    enabled: !!workspaceId,
  });
  const file = useQuery({
    queryKey: ['file', workspaceId, path],
    queryFn: () => api.workspaces.readFile(workspaceId as string, path as string),
    enabled: !!workspaceId && !!path,
  });
  const save = useMutation({
    mutationFn: () =>
      api.workspaces.saveFile(workspaceId as string, path as string, draft ?? '', file.data?.hash),
    onSuccess: async () => {
      setDraft(null);
      setConflict(false);
      await client.invalidateQueries({ queryKey: ['file', workspaceId, path] });
    },
    onError: (error) => setConflict(error instanceof ApiError && error.code === 'FILE_CONFLICT'),
  });

  if (!workspaceId) {
    return <div className="p-6 text-center text-sm text-mute">选择工作区后可浏览文件</div>;
  }
  const dirty = draft !== null && draft !== file.data?.content;

  return (
    <div className="flex h-full flex-col">
      <div className="max-h-52 shrink-0 overflow-auto border-b border-line/70 p-1.5">
        {tree.isLoading && <p className="p-2 text-xs text-mute">读取文件树…</p>}
        <ul>
          {tree.data?.children?.map((c) => (
            <Tree
              key={c.path}
              node={c}
              depth={0}
              selected={path}
              onOpen={(p) => {
                setPath(p);
                setDraft(null);
                setConflict(false);
              }}
            />
          ))}
        </ul>
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        {path ? (
          <>
            <div className="flex items-center gap-2 border-b border-line/70 bg-ink/[0.02] px-3 py-1.5 text-sm">
              <FileGlyph name={path} size={14} />
              <span className="min-w-0 flex-1 truncate font-mono text-xs" title={path}>
                {path}
              </span>
              {dirty && <span className="h-1.5 w-1.5 rounded-full bg-accent" title="未保存" />}
              <Button
                variant="primary"
                className="px-2.5 py-1 text-xs"
                disabled={!dirty || save.isPending}
                onClick={() => save.mutate()}
              >
                保存
              </Button>
            </div>
            {conflict && (
              <div
                role="alert"
                className="flex flex-wrap items-center gap-2 bg-warn/10 px-3 py-2 text-xs text-warn"
              >
                文件在你打开之后被改动过（可能是 Agent）。
                <Button
                  onClick={async () => {
                    setConflict(false);
                    setDraft(null);
                    await file.refetch();
                  }}
                >
                  放弃我的修改并重新载入
                </Button>
              </div>
            )}
            {file.isError ? (
              <p className="p-3 text-sm text-bad">{(file.error as Error).message}</p>
            ) : (
              <textarea
                aria-label="文件内容"
                spellCheck={false}
                className="min-h-0 flex-1 resize-none bg-transparent p-3 font-mono text-xs leading-relaxed outline-none"
                value={draft ?? stripControl(file.data?.content ?? '')}
                onChange={(e) => setDraft(e.target.value)}
              />
            )}
          </>
        ) : (
          <p className="p-6 text-center text-sm text-mute">从上方文件树选择一个文件</p>
        )}
      </div>
    </div>
  );
}
