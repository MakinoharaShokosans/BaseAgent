import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { ArrowUp, ChevronRight } from 'lucide-react';
import { FolderGlyph } from '../icons/Glyphs';
import { Button } from '../ui/primitives';

export function DirectoryPicker({ onPick }: { onPick: (path: string) => void }) {
  const [path, setPath] = useState<string | undefined>(undefined);
  const listing = useQuery({
    queryKey: ['directories', path],
    queryFn: () => api.system.directories(path),
  });
  if (listing.isError) {
    return <p className="text-sm text-bad">{(listing.error as Error).message}</p>;
  }
  const data = listing.data;
  return (
    <div className="overflow-hidden rounded-2xl bg-ink/[0.035] border border-ink/[0.06] dark:border-white/[0.07]">
      <div className="flex items-center gap-2 bg-ink/[0.04] px-2 py-1.5 text-xs">
        <button
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 font-semibold hover:bg-ink/[0.06] disabled:text-mute/40"
          disabled={!data?.parent}
          onClick={() => setPath(data?.parent ?? undefined)}
        >
          <ArrowUp size={12} /> 上级
        </button>
        <span className="min-w-0 flex-1 truncate font-mono" title={data?.path}>
          {data?.path ?? '…'}
        </span>
        <Button
          variant="primary"
          className="px-2.5 py-0.5 text-xs"
          disabled={!data}
          onClick={() => data && onPick(data.path)}
        >
          选择此目录
        </Button>
      </div>
      <ul className="max-h-52 overflow-auto p-1 text-sm">
        {data?.entries.map((e) => (
          <li key={e.path}>
            <button
              className="flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-ink/[0.05]"
              onClick={() => setPath(e.path)}
            >
              <FolderGlyph open={false} size={15} />
              <span className="min-w-0 flex-1 truncate">{e.name}</span>
              {e.has_children && <ChevronRight size={13} className="text-mute" />}
            </button>
          </li>
        ))}
        {data && !data.entries.length && <li className="px-2 py-1 text-mute">没有子目录</li>}
        {data?.truncated && <li className="px-2 py-1 text-xs text-mute">条目过多，已截断</li>}
      </ul>
    </div>
  );
}
