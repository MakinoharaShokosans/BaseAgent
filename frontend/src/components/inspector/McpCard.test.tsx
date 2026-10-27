import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/endpoints';
import type { McpServer } from '../../types/api';
import { McpCard } from './McpCard';

const base: McpServer = {
  name: 'x',
  transport: 'stdio',
  status: 'disabled',
  tool_count: 0,
  tools: [],
  rejected: [],
  last_error: '',
  retry_in_sec: 0,
};

function mount(servers: McpServer[]) {
  vi.spyOn(api.system, 'mcpServers').mockResolvedValue(servers);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <McpCard />
    </QueryClientProvider>,
  );
}

describe('McpCard', () => {
  afterEach(() => vi.restoreAllMocks());

  it('explains how to enable servers when none is enabled', async () => {
    mount([base]);
    expect(await screen.findByText(/没有启用的服务器/)).toBeInTheDocument();
  });

  it('shows connected servers with tool counts and rejected tools, and failures with the retry time', async () => {
    mount([
      base,
      {
        ...base,
        name: 'github',
        status: 'connected',
        tool_count: 3,
        rejected: [{ tool: 'p', reason: '描述含疑似注入样态' }],
      },
      { ...base, name: 'docs', status: 'failed', last_error: 'TimeoutError', retry_in_sec: 30 },
    ]);
    expect(await screen.findByText('3 个工具，1 个被拒绝')).toBeInTheDocument();
    expect(screen.getByText('已连接')).toBeInTheDocument();
    expect(screen.getByText('TimeoutError（30 秒后重试）')).toBeInTheDocument();
    expect(screen.queryByText('未启用')).not.toBeInTheDocument();
  });
});
