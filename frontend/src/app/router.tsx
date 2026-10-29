import {
  Navigate,
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from '@tanstack/react-router';
import { ConversationPage } from '../features/conversation/ConversationPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { WorkspacePage } from '../features/workspace/WorkspacePage';
import { Home } from './Home';
import { Shell } from './Shell';

const root = createRootRoute({ component: Shell });

const home = createRoute({ getParentRoute: () => root, path: '/', component: Home });
const workspace = createRoute({
  getParentRoute: () => root,
  path: '/w/$workspaceId',
  component: Outlet,
});
const workspaceIndex = createRoute({
  getParentRoute: () => workspace,
  path: '/',
  component: WorkspacePage,
});
const session = createRoute({
  getParentRoute: () => workspace,
  path: '/s/$sessionId',
  component: ConversationPage,
  // ?task=:id 定位到某个历史任务，刷新与分享链接都能回到同一次运行
  validateSearch: (search: Record<string, unknown>) => ({
    task: typeof search['task'] === 'string' ? search['task'] : undefined,
  }),
});
const settings = createRoute({
  getParentRoute: () => root,
  path: '/settings',
  component: Outlet,
});
const settingsIndex = createRoute({
  getParentRoute: () => settings,
  path: '/',
  component: SettingsPage,
});
const settingsSection = createRoute({
  getParentRoute: () => settings,
  path: '$section',
  component: SettingsPage,
});
const fallback = createRoute({
  getParentRoute: () => root,
  path: '*',
  component: () => <Navigate to="/" replace />,
});

const routeTree = root.addChildren([
  home,
  workspace.addChildren([workspaceIndex, session]),
  settings.addChildren([settingsIndex, settingsSection]),
  fallback,
]);

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history, defaultPreload: false });
}

export type AppRouter = ReturnType<typeof createAppRouter>;
