import { useQuery } from '@tanstack/react-query';
import { api } from '../../../api/endpoints';
import { formatTokens } from '../../../utils/text';
import { Group, Row } from '../parts';

export function Models() {
  const models = useQuery({
    queryKey: ['models-raw'],
    queryFn: api.system.models,
    staleTime: Infinity,
  });
  const tiers = new Map<string, NonNullable<typeof models.data>>();
  for (const m of models.data ?? []) tiers.set(m.tier, [...(tiers.get(m.tier) ?? []), m]);
  return (
    <>
      {[...tiers.entries()].map(([tier, list]) => (
        <Group key={tier} title={tier === 'reasoning' ? '推理' : tier === 'fast' ? '快速' : tier}>
          {list.map((m, i) => (
            <Row
              key={m.name}
              title={<span className="font-medium">{m.name}</span>}
              note={<span className="font-mono">{m.model}</span>}
            >
              <span className="text-xs tabular-nums text-mute">
                {formatTokens(m.context_window)}
              </span>
              <span className="w-4 text-center text-2xs text-mute">{i + 1}</span>
            </Row>
          ))}
        </Group>
      ))}
      {models.isError && <p className="text-sm text-bad">{(models.error as Error).message}</p>}
    </>
  );
}
