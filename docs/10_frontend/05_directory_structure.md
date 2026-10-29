# 前端代码目录与快速定位指南

本文档与 [`frontend/DIRECTORY_GUIDE.md`](../../frontend/DIRECTORY_GUIDE.md) 同步，为开发人员与 Agent 提供在修改前端时快速定位源文件的查阅索引。

---

## 1. 界面区域与核心组件映射

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                     [Shell.tsx]                                        │
├──────────────┬──────────────────────────────────────────────────────────┬──────────────┤
│              │ [ConversationPage.tsx] (顶栏: 面包屑 / MinimalStatusMark)   │              │
│              ├──────────────────────────────────────────────────────────┤              │
│              │ [RunView.tsx] (进行中任务运行流 / 步骤列表)                   │ [Inspector]  │
│              │   ├─ [blocks/SayBlock.tsx] (助手回复 / Markdown 渲染)       │              │
│  [Sidebar]   │   ├─ [blocks/ReasoningBlock.tsx] (思考链展开折叠)         │ [TaskCard]   │
│              │   ├─ [blocks/ToolCard.tsx] (Bash / Edit 等工具调用卡片)   │ (指标/计划项) │
│ (工作区树/   │   └─ [domain/HitlApprovalCard.tsx] (人机审批卡片)        │              │
│  会话列表/   ├──────────────────────────────────────────────────────────┤ [ContextCard]│
│  下划线动效/ │ [Composer.tsx]                                           │ (Token 占比/ │
│  未读绿点)   │ (多行输入框 / 模型选择 / 权限等级 / 发送队列)                │  记忆分布)   │
├──────────────┴──────────────────────────────────────────────────────────┴──────────────┤
│ 独立全屏路由: [SettingsPage.tsx] (General / Models / Prompts / Skills / Tools / Mcp)    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. “按改动需求”秒查索引表

| 需求场景 | 目标文件 | 关键逻辑 / 状态点 |
|---|---|---|
| **改动侧栏选择动效 / 字体动态下划线** | [`frontend/src/features/sidebar/Sidebar.tsx`](../../frontend/src/features/sidebar/Sidebar.tsx) | `active-workspace-underline`, `active-session-underline`, `layoutId` 动效 |
| **改动会话未读消除 / 触底已读** | [`frontend/src/features/sidebar/Sidebar.tsx`](../../frontend/src/features/sidebar/Sidebar.tsx)<br>[`frontend/src/stores/useUiStore.ts`](../../frontend/src/stores/useUiStore.ts)<br>[`frontend/src/features/conversation/ConversationPage.tsx`](../../frontend/src/features/conversation/ConversationPage.tsx) | `readSessions`, `markSessionRead`, `isRead = readAt >= activity.at`, `scroller onScroll` |
| **改动任务卡指标 (回合/工具/Token/用时)** | [`frontend/src/features/inspector/TaskCard.tsx`](../../frontend/src/features/inspector/TaskCard.tsx) | SVG 图标排版、耗时实时刷新、交付产物过滤 |
| **改动任务状态图标 (对钩/排队/失败)** | [`frontend/src/utils/status.tsx`](../../frontend/src/utils/status.tsx)<br>[`frontend/src/components/ui/StatusDot.tsx`](../../frontend/src/components/ui/StatusDot.tsx) | `MinimalStatusMark`, `StatusPill`, `statusMeta`, `StatusDot` |
| **改动命令行工具卡片 (Bash/输出/折叠)** | [`frontend/src/features/conversation/blocks/ToolCard.tsx`](../../frontend/src/features/conversation/blocks/ToolCard.tsx) | stdout/stderr 实时追加、展开/折叠、执行耗时、状态标识 |
| **改动思考链 (Reasoning / 深度思考)** | [`frontend/src/features/conversation/blocks/ReasoningBlock.tsx`](../../frontend/src/features/conversation/blocks/ReasoningBlock.tsx) | 自动折叠偏好 `thinking`、耗时统计 |
| **改动助手回复 / Markdown 样式** | [`frontend/src/features/conversation/blocks/SayBlock.tsx`](../../frontend/src/features/conversation/blocks/SayBlock.tsx)<br>[`frontend/src/components/common/SafeMarkdown.tsx`](../../frontend/src/components/common/SafeMarkdown.tsx) | 代码高亮、行号、复制按钮、公式 KaTeX、无 raw HTML 注入 |
| **改动人机交互审批 (HITL Approve/Reject)** | [`frontend/src/components/domain/HitlApprovalCard.tsx`](../../frontend/src/components/domain/HitlApprovalCard.tsx) | 越级命令确认、一次性通过、永久允许、拒绝并附带理由输入 |
| **改动输入框 / 快捷键 / 提示词发送** | [`frontend/src/features/conversation/Composer.tsx`](../../frontend/src/features/conversation/Composer.tsx) | `Enter` 发送 / `Shift+Enter` 换行、模型选择器、权限等级、排队处理 |
| **改动系统提示词编辑器** | [`frontend/src/features/settings/sections/Prompts.tsx`](../../frontend/src/features/settings/sections/Prompts.tsx) | 行号渲染槽、查找对话框 (`Ctrl+F`)、Token 估算公式、只读/重置模板 |
| **改动技能 / MCP 服务管理** | [`frontend/src/features/settings/sections/Skills.tsx`](../../frontend/src/features/settings/sections/Skills.tsx)<br>[`frontend/src/features/settings/sections/Mcp.tsx`](../../frontend/src/features/settings/sections/Mcp.tsx) | 即时模糊搜索过滤、开关切换、连接状态与工具一览 |
| **改动路由树 / 页面跳转恢复** | [`frontend/src/app/router.tsx`](../../frontend/src/app/router.tsx)<br>[`frontend/src/utils/lastLocation.ts`](../../frontend/src/utils/lastLocation.ts) | `/` -> `/w/:wid/s/:sid` 恢复、`/settings/:section` 路由映射 |
| **改动全局主题色 / 暗黑模式 / 字体** | [`frontend/src/index.css`](../../frontend/src/index.css)<br>[`frontend/src/components/layout/ThemeToggle.tsx`](../../frontend/src/components/layout/ThemeToggle.tsx) | `--c-canvas`, `--c-ink`, `--c-accent`, `--brand` 变量、`resolvedDark` |
| **改动 SSE 事件订阅 / 实时通讯** | [`frontend/src/api/sse.ts`](../../frontend/src/api/sse.ts)<br>[`frontend/src/stores/useAppStore.ts`](../../frontend/src/stores/useAppStore.ts)<br>[`frontend/src/stores/taskReducer.ts`](../../frontend/src/stores/taskReducer.ts) | `subscribeGlobal`（全局总线）、`subscribe`（任务事件流）、事件状态机归约 |

---

## 3. 架构分层

1. **路由与外壳 (`app/`)**：URL 是唯一真实状态源，外壳控制响应式三栏与抽屉伸缩。
2. **业务功能 (`features/`)**：按屏幕视区与主要页面划分独立模块，高内聚，零跨功能深层耦合。
3. **状态管理 (`stores/`)**：`useAppStore` 负责任务执行，`useUiStore` 负责本地偏好，`useQueueStore` 负责任务队列。
4. **服务端通信 (`api/`)**：基于 TanStack Query 做跨页面数据共享缓存，基于 `sse.ts` 做增量实时推流。
5. **基础组件库 (`components/`)**：`ui/` 是极简底层控件，`domain/` 是业务级复用控件（如审批卡），`modals/` 是确认与输入弹窗。
