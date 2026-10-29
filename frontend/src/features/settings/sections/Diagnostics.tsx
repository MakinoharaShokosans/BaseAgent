import { useQuery } from '@tanstack/react-query';
import { api } from '../../../api/endpoints';
import { cn } from '../../../utils/text';
import { Group, Row } from '../parts';

export function Diagnostics() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: api.system.health,
    refetchInterval: 10_000,
  });
  const limits = useQuery({ queryKey: ['limits'], queryFn: api.system.limits });
  return (
    <>
      <Group title="依赖">
        {Object.entries(health.data ?? {}).map(([name, status]) => (
          <Row key={name} title={<span className="font-mono text-[13px]">{name}</span>}>
            <span
              className={cn(
                'flex items-center gap-1.5 text-xs',
                status === 'ok' ? 'text-ok' : 'text-bad',
              )}
            >
              <span className={cn('h-2 w-2 rounded-full', status === 'ok' ? 'bg-ok' : 'bg-bad')} />
              {status}
            </span>
          </Row>
        ))}
        {health.isError && (
          <p className="py-3 text-sm text-bad">{(health.error as Error).message}</p>
        )}
      </Group>
      {limits.data && (
        <Group title="限制">
          <Row title="并发任务">
            <span className="tabular-nums">{limits.data.max_concurrent_tasks}</span>
          </Row>
        </Group>
      )}
    </>
  );
}
