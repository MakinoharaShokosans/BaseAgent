import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider, type RouterHistory } from '@tanstack/react-router';
import { useState } from 'react';
import { createAppRouter } from './app/router';

/** 应用入口。快照走 TanStack Query，任务进度走 SSE（见 stores/useAppStore），位置由路由决定。 */
export default function App({ history }: { history?: RouterHistory }) {
  const [client] = useState(
    () =>
      new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } }),
  );
  const [router] = useState(() => createAppRouter(history));
  return (
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
