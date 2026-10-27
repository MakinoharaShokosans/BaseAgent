import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { AppLayout } from './components/layout/AppLayout';

/** 应用入口。快照走 TanStack Query，任务进度走 SSE（见 stores/useAppStore）。 */
export default function App() {
  const [client] = useState(
    () =>
      new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } }),
  );
  return (
    <QueryClientProvider client={client}>
      <AppLayout />
    </QueryClientProvider>
  );
}
