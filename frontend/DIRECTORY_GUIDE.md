# 前端目录结构与快速定位指南 (Frontend Directory Guide)

> **定位宗旨**：无需每次通篇搜索或分析调用栈。本指南按**“界面视觉区域”**与**“功能需求场景”**双向索引，助你在 5 秒内精准定位到目标文件。

---

## 1. 按界面视觉区域秒查 (Visual Area Map)

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

| 界面区域 | 对应核心入口组件 | 内部子组件 / 辅助文件 | 核心职责 |
|---|---|---|---|
| **左侧导航栏** | [`features/sidebar/Sidebar.tsx`](src/features/sidebar/Sidebar.tsx) | [`components/ui/StatusDot.tsx`](src/components/ui/StatusDot.tsx)<br>[`components/modals/WorkspaceModal.tsx`](src/components/modals/WorkspaceModal.tsx) | 工作区列表、会话树、搜索、字体动态下划线、未读消除、新建/删除/分叉会话 |
| **中央对话流** | [`features/conversation/ConversationPage.tsx`](src/features/conversation/ConversationPage.tsx) | [`features/conversation/RunView.tsx`](src/features/conversation/RunView.tsx)<br>[`features/conversation/useReplay.ts`](src/features/conversation/useReplay.ts) | 消息流呈现、自动滚动与“回到最新”、任务历史回放、首个任务自动生成标题 |
| **消息/步骤块** | [`features/conversation/blocks/`](src/features/conversation/blocks/) | [`SayBlock.tsx`](src/features/conversation/blocks/SayBlock.tsx)<br>[`ReasoningBlock.tsx`](src/features/conversation/blocks/ReasoningBlock.tsx)<br>[`ToolCard.tsx`](src/features/conversation/blocks/ToolCard.tsx)<br>[`AgentGroup.tsx`](src/features/conversation/blocks/AgentGroup.tsx) | 单条消息内容渲染、深度思考折叠展开、终端/编辑工具调用卡片及实时日志流、子智能体嵌套分组 |
| **底部输入区** | [`features/conversation/Composer.tsx`](src/features/conversation/Composer.tsx) | [`components/ui/Select.tsx`](src/components/ui/Select.tsx)<br>[`stores/useQueueStore.ts`](src/stores/useQueueStore.ts) | 多行自动伸缩输入框、快捷键、模型切换、权限挡位 (ask/auto/bypass)、排队与待生效提示 |
| **右侧检视器** | [`features/inspector/Inspector.tsx`](src/features/inspector/Inspector.tsx) | [`TaskCard.tsx`](src/features/inspector/TaskCard.tsx)<br>[`ContextCard.tsx`](src/features/inspector/ContextCard.tsx) | 任务指标（回合/工具/Token/用时 SVG）、计划项 Checklist 跳转定位、产物列表、上下文窗口与记忆统计 |
| **设置中心** | [`features/settings/SettingsPage.tsx`](src/features/settings/SettingsPage.tsx) | [`sections/Prompts.tsx`](src/features/settings/sections/Prompts.tsx)<br>[`sections/Skills.tsx`](src/features/settings/sections/Skills.tsx)<br>[`sections/Mcp.tsx`](src/features/settings/sections/Mcp.tsx)<br>[`sections/Models.tsx`](src/features/settings/sections/Models.tsx)<br>[`sections/General.tsx`](src/features/settings/sections/General.tsx)<br>[`sections/Tools.tsx`](src/features/settings/sections/Tools.tsx)<br>[`sections/Diagnostics.tsx`](src/features/settings/sections/Diagnostics.tsx) | 全屏设置抽屉：提示词专业代码编辑（行号+查找+Token预估）、技能检索开关、MCP 增删改查、模型服务商配置 |
| **工作区概览** | [`features/workspace/WorkspacePage.tsx`](src/features/workspace/WorkspacePage.tsx) | - | 单击工作区根节点呈现的工程元信息、根目录路径、持久化记忆一览 |

---

## 2. 按“改动需求”秒查索引表 (Task-to-File Lookup)

当你接到一个具体修改任务时，查阅下表直达对应文件：

| 需求场景 | 目标文件 | 关键逻辑 / 状态点 |
|---|---|---|
| **改动侧栏选择动效 / 下划线 / 激活状态** | [`src/features/sidebar/Sidebar.tsx`](src/features/sidebar/Sidebar.tsx) | `active-workspace-underline`, `active-session-underline`, `layoutId` 动效 |
| **改动会话未读红绿点 / 消除逻辑** | [`src/features/sidebar/Sidebar.tsx`](src/features/sidebar/Sidebar.tsx)<br>[`src/stores/useUiStore.ts`](src/stores/useUiStore.ts)<br>[`src/features/conversation/ConversationPage.tsx`](src/features/conversation/ConversationPage.tsx) | `readSessions`, `markSessionRead`, `isRead = readAt >= activity.at`, `scroller onScroll` |
| **改动任务卡指标 (回合/工具/Token/用时)** | [`src/features/inspector/TaskCard.tsx`](src/features/inspector/TaskCard.tsx) | SVG 图标排版、耗时实时刷新、交付产物过滤 |
| **改动任务状态图标 (对钩/排队/失败/药丸)** | [`src/utils/status.tsx`](src/utils/status.tsx)<br>[`src/components/ui/StatusDot.tsx`](src/components/ui/StatusDot.tsx) | `MinimalStatusMark`, `StatusPill`, `statusMeta`, `StatusDot` |
| **改动命令行工具卡片 (Bash/输出/折叠)** | [`src/features/conversation/blocks/ToolCard.tsx`](src/features/conversation/blocks/ToolCard.tsx) | stdout/stderr 实时追加、展开/折叠、执行耗时、状态标识 |
| **改动思考链 (Reasoning / 深度思考)** | [`src/features/conversation/blocks/ReasoningBlock.tsx`](src/features/conversation/blocks/ReasoningBlock.tsx) | 自动折叠偏好 `thinking: 'expanded' \| 'collapsed' \| 'hidden'`、展开耗时 |
| **改动助手回复 / Markdown 样式** | [`src/features/conversation/blocks/SayBlock.tsx`](src/features/conversation/blocks/SayBlock.tsx)<br>[`src/components/common/SafeMarkdown.tsx`](src/components/common/SafeMarkdown.tsx) | 代码高亮、行号、复制按钮、公式 KaTeX、无 raw HTML 注入 |
| **改动人机交互审批 (HITL Approve/Reject)** | [`src/components/domain/HitlApprovalCard.tsx`](src/components/domain/HitlApprovalCard.tsx) | 越级命令确认、一次性通过、永久允许、拒绝并附带理由输入 |
| **改动输入框 / 快捷键 / 提示词发送** | [`src/features/conversation/Composer.tsx`](src/features/conversation/Composer.tsx) | `Enter` 发送 / `Shift+Enter` 换行、模型选择器、权限等级、排队处理 |
| **改动系统提示词编辑器** | [`src/features/settings/sections/Prompts.tsx`](src/features/settings/sections/Prompts.tsx) | 行号渲染槽、查找对话框 (`Ctrl+F`)、Token 估算公式、只读/重置模板 |
| **改动技能 / MCP 服务管理** | [`src/features/settings/sections/Skills.tsx`](src/features/settings/sections/Skills.tsx)<br>[`src/features/settings/sections/Mcp.tsx`](src/features/settings/sections/Mcp.tsx) | 即时模糊搜索过滤、开关切换、连接状态与工具一览 |
| **改动路由树 / 页面跳转恢复** | [`src/app/router.tsx`](src/app/router.tsx)<br>[`src/utils/lastLocation.ts`](src/utils/lastLocation.ts) | `/` -> `/w/:wid/s/:sid` 恢复、`/settings/:section` 路由映射 |
| **改动全局主题色 / 暗黑模式 / 字体** | [`src/index.css`](src/index.css)<br>[`src/components/layout/ThemeToggle.tsx`](src/components/layout/ThemeToggle.tsx) | `--c-canvas`, `--c-ink`, `--c-accent`, `--brand` 变量、`resolvedDark` |
| **改动 SSE 事件订阅 / 实时通讯** | [`src/api/sse.ts`](src/api/sse.ts)<br>[`src/stores/useAppStore.ts`](src/stores/useAppStore.ts)<br>[`src/stores/taskReducer.ts`](src/stores/taskReducer.ts) | `subscribeGlobal`（全局总线）、`subscribe`（任务事件流）、事件状态机归约 |

---

## 3. 完整目录树与职责一览 (Directory Tree)

```
frontend/src/
├── main.tsx                      # Web 入口：React DOM 挂载、QueryClient、ErrorBoundary
├── App.tsx                       # 全局外壳：路由加载与全局 Toast 提示器
├── index.css                     # 主题令牌（CSS Variables）、极简无框阴影、动画定义
│
├── api/                          # 【数据请求层】
│   ├── http.ts                   # Fetch 基础封装：URL 构建、Bearer 认证、标准化错误提炼
│   ├── sse.ts                    # SSE 客户端：支持断线退避重连、序号补缺、全局总线广播
│   ├── endpoints.ts              # 后端 REST 接口映射（workspaces / sessions / tasks / settings 等）
│   ├── queries.ts                # TanStack Query 常用查询 Hooks（多处共享会话活动与任务缓存）
│   └── useSettings.ts            # 系统设置、模型列表、上下文窗口快照的 Query 聚合
│
├── app/                          # 【路由与主框架】
│   ├── router.tsx                # TanStack Router 路由定义（/、/w/:wid、/w/:wid/s/:sid、/settings/:section）
│   ├── Shell.tsx                 # 主工作台三栏布局骨架（侧栏 + 对话区 + 检视器 + 全屏抽屉）
│   └── Home.tsx                  # 根路径重定向：自动回弹到用户上次停留的工作区或会话
│
├── components/                   # 【通用与领域组件库】
│   ├── ui/                       # 基础无样式 / 极简基础控件
│   │   ├── primitives.tsx        # IconButton、Card、Reveal（平滑折叠展开容器）
│   │   ├── StatusDot.tsx         # 多态状态圆点（运行微光光晕、警告黄点、完成绿点）
│   │   ├── Toggle.tsx            # 平滑滑动开关（Segmented 分段控制器）
│   │   ├── InlineEdit.tsx        # 双击/聚焦就地重命名输入框
│   │   ├── Select.tsx            # 自定义轻量下拉单选
│   │   ├── Slider.tsx            # 精密刻度滑块
│   │   ├── Checkbox.tsx          # 简约复选框
│   │   ├── Tooltip.tsx           # 高灵敏悬浮气泡
│   │   ├── CopyButton.tsx        # 一键复制代码/文本并附带反馈打钩
│   │   └── MoreMenu.tsx          # 三点更多操作下拉气泡菜单
│   ├── common/                   # 通用业务基础件
│   │   ├── SafeMarkdown.tsx      # 安全 Markdown：代码语法高亮、LaTeX 公式、禁用原始 HTML
│   │   └── DirectoryPicker.tsx   # 服务端文件目录浏览器（选择工作区根目录）
│   ├── domain/                   # 强业务领域件
│   │   ├── HitlApprovalCard.tsx  # 人机协同审批卡片（命令、破坏性影响提示、单次/永久按钮）
│   │   └── McpStatus.tsx         # 顶栏 MCP 服务状态小图标与指示器
│   ├── modals/                   # 弹窗体系
│   │   ├── WorkspaceModal.tsx    # 新建工作区模态框
│   │   ├── RenameWorkspaceModal.tsx # 工作区重命名模态框
│   │   └── ConfirmDialog.tsx     # 高危二次确认弹窗（支持输入名称校验防误删）
│   ├── layout/                   # 布局附属件
│   │   └── ThemeToggle.tsx       # 浅色 / 深色主题切换器
│   └── icons/                    # 自定义矢量图标
│       └── Glyphs.tsx            # 特殊业务符号 SVG
│
├── features/                     # 【核心功能模块 - 按页面/区域划分】
│   ├── sidebar/                  # 侧边栏
│   │   └── Sidebar.tsx           # 展开抽屉/折叠图标条、工作区树、会话树、搜索、已读消除、下划线
│   ├── conversation/             # 对话与执行流
│   │   ├── ConversationPage.tsx  # 消息列表渲染、触底感知、自动标记已读、标题自动生成
│   │   ├── Composer.tsx          # 输入框、快捷键、模型切换、权限设定、任务排队
│   │   ├── RunView.tsx           # 任务实时执行流看板、回合步骤拆解
│   │   ├── useReplay.ts          # 历史任务回放状态与单步推进器
│   │   └── blocks/               # 消息与执行积木块
│   │       ├── SayBlock.tsx      # Agent 文本回复与 Markdown
│   │       ├── ReasoningBlock.tsx# 思考链（Thinking/Reasoning）展开折叠
│   │       ├── ToolCard.tsx      # 工具执行卡片（Bash、Edit、View 及 stdout 增量流）
│   │       └── AgentGroup.tsx    # 子智能体委派与多智能体生命周期容器
│   ├── inspector/                # 右侧检视抽屉
│   │   ├── Inspector.tsx         # 检视器容器骨架与折叠/展开联动
│   │   ├── TaskCard.tsx          # 任务统计（SVG 指标无文字重叠）、规划进度锚点定位、产物列表
│   │   └── ContextCard.tsx       # 上下文 Token 限额图表、长期/短期记忆占比
│   ├── workspace/                # 工作区视图
│   │   └── WorkspacePage.tsx     # 单击工作区时呈现的项目看板、元数据与记忆设置
│   └── settings/                 # 设置中心
│       ├── SettingsPage.tsx      # 全屏设置页骨架与侧边 Tab 分页
│       ├── parts.tsx             # 设置页通用表单块、分组卡片、标题排版
│       └── sections/             # 各设置子项
│           ├── General.tsx       # 基础通用偏好（主题模式、默认思考链展示等）
│           ├── Models.tsx        # 模型供应商与 API Key 配置（OpenAI / DeepSeek / Anthropic 等）
│           ├── Prompts.tsx       # 系统提示词配置（专业代码行号编辑器、Token 估算、模板重置）
│           ├── Skills.tsx        # 技能库管理（搜索、启用/停用开关、描述）
│           ├── Mcp.tsx           # MCP 工具集成（SSE/stdio 协议配置、在线探测）
│           ├── Tools.tsx         # 内置工具审批安全策略
│           └── Diagnostics.tsx   # 运行时诊断（SQLite 状态、健康检查）
│
├── stores/                       # 【状态中心】
│   ├── useAppStore.ts            # 全局运行时状态：当前活跃任务、SSE 连接、全局通知 Toast、提交/审批动作
│   ├── useUiStore.ts             # 本地界面偏好：侧栏开合、检视器开合、工作区折叠展开、会话已读记录 (`readSessions`)
│   ├── useQueueStore.ts          # 任务队列（本地排队与按序消费）
│   ├── usePendingStore.ts        # 待生效操作计数器
│   └── taskReducer.ts            # 纯函数：将后端实时 SSE 事件归约计算为最新 RunState
│
├── types/                        # 【TypeScript 类型契约】
│   └── api.ts                    # 与后端 OpenAPI 完全对齐的 TypeScript 接口与枚举
│
└── utils/                        # 【工具函数】
    ├── status.tsx                # 任务状态（succeeded/failed/running...）的文字、颜色与极简打钩图标映射
    ├── text.ts                   # 文本处理：`cn` (clsx + tailwind-merge)、截断处理
    ├── time.ts                   # 时间格式化：相对时间（刚刚、几分钟前）、执行耗时计算
    ├── diff.ts                   # 文本/代码 Diff 解析与行内比较
    ├── lastLocation.ts           # 记录用户最后访问的工作区与会话（刷新/重新打开自动恢复）
    └── useNow.ts                 # 高性能秒级定时器 Hook（驱动运行中任务的“用时 xx 秒”实时刷新）
```

---

## 4. 核心状态与数据流 (State Architecture)

1. **路由即权威 (URL is Single Source of Truth)**
   - 工作区 ID 与会话 ID 严格由 TanStack Router 管理（`/w/:workspaceId/s/:sessionId`）。
   - **不要**在 store 里重复保存当前选中的 workspace/session，组件内统一通过 `useParams()` 获取。
2. **状态分层原则**
   - **服务端状态 (Server State)**：使用 **TanStack Query**（`api/queries.ts`）。全应用共享缓存，跨组件绝不发重复网络请求。
   - **执行运行时状态 (Live Task Runtime)**：使用 **`useAppStore` + `taskReducer`**。单任务执行期间通过 SSE 增量推流，经 reducer 纯函数计算后更新至全局 store。
   - **跨会话通知 (Global Bus)**：使用 **`subscribeGlobal`**。服务端有任务变动广播时，侧栏监听自动触发 React Query 失效刷新。
   - **界面交互偏好 (Local UI Prefs)**：使用 **`useUiStore`**。开合状态、主题、已读时间戳自动持久化至 `localStorage ('baseagent.ui.v2')`。

---

## 5. 快速修改准则与避坑要点 (Rules of Thumb)

1. **极简风设计规约（简约风）**：
   - 避免使用大面积矩形色块；背景以通透、透明、无边框（`backdrop-blur`）为主。
   - 状态完成仅用无底色绿色对钩 (`<MinimalStatusMark />`)，避免药丸容器与冗余文字。
   - 选中下划线应紧密包裹在字体（`<span className="relative min-w-0 max-w-full">`）下方，严禁整行拉通。
2. **验证必须纯无浏览器执行**：
   - 类型验证：`pnpm --dir frontend typecheck`
   - 代码风格：`pnpm --dir frontend lint`
   - 单元测试：`pnpm --dir frontend test --run`
   - 打包构建：`pnpm --dir frontend build`
