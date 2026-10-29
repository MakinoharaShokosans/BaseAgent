import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../../../api/endpoints';
import { Chevron, Reveal } from '../../../components/ui/primitives';
import { Group } from '../parts';

export function Tools() {
  const tools = useQuery({ queryKey: ['tools'], queryFn: api.system.tools });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Group>
      {(tools.data ?? []).map((t) => (
        <div key={t.name} className="py-2.5">
          <button
            aria-expanded={open === t.name}
            onClick={() => setOpen(open === t.name ? null : t.name)}
            className="flex w-full items-center gap-2 text-left"
          >
            <span className="font-mono text-[13px] font-medium">{t.name}</span>
            <span className="min-w-0 flex-1 truncate text-[12.5px] text-mute">{t.description}</span>
            <Chevron open={open === t.name} size={12} />
          </button>
          <Reveal open={open === t.name}>
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
              {JSON.stringify(t.parameters, null, 2)}
            </pre>
          </Reveal>
        </div>
      ))}
      {tools.isError && <p className="py-3 text-sm text-bad">{(tools.error as Error).message}</p>}
      {tools.isPending && <p className="py-3 text-[13px] text-mute">—</p>}
    </Group>
  );
}
