# 前端控制台

用户与 Agent 之间唯一的界面：看进度、做审批、管理工作区与记忆。前端是服务端状态的投影，不持有业务事实的权威副本，也不做任何编排决策。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [01_architecture.md](01_architecture.md) | 技术栈、目录、布局、数据流、与后端契约的对照、新版设计要点 | 已写初稿 |
| [02_approval_and_safety_ux.md](02_approval_and_safety_ux.md) | 审批卡片、权限基线选择、信任可视化、渲染不可信内容、破坏性操作确认 | 已写初稿 |
| [03_visual_design.md](03_visual_design.md) | 视觉语言：无边框浅色风格、字体与色彩、侧栏与设置、自定义下拉与滑动条、模型选择、对话节点、右侧卡片、上下文占比、动效与图标 | 已实现 |

## 前端承担的安全职责

前端是最后一道门：它决定用户能不能看清自己在批准什么，也决定来自模型和网页的内容会不会在用户的浏览器里被当作代码执行。所以安全相关的界面行为都当作**契约**，有测试固定下来。

## 现状发现的问题

| # | 问题 | 位置 | 级别 |
|---|---|---|---|
| 1 | 前端解析不了后端的错误信封，用户只看到通用错误文案 | [01 §7](01_architecture.md) | 高 |
| 2 | SSE 断开后不重连，事件流永久中断且用户不知情 | [01 §7](01_architecture.md) | 高 |
| 3 | 令牌同时放在请求头和 URL 查询参数里，且存放在 `localStorage` | [01 §7](01_architecture.md) | 中 |
| 4 | 没有序号连续性检查，也不处理 `STREAM_GAP` | [01 §7](01_architecture.md) | 中 |
| 5 | `llm.call` 整体进入 store，长任务内存增长 | [01 §7](01_architecture.md) | 中 |
| 6 | 审批卡片：批准按钮可被快速误触，"免审"缺少解释，一批多项动作的信息不完整 | [02 §1](02_approval_and_safety_ux.md) | 中 |
| 7 | `full_permissions` 可直接选择，无二次确认与持续提示 | [02 §2](02_approval_and_safety_ux.md) | 中 |
| 8 | 刷新页面后运行中任务的界面状态无法恢复 | [01 §7](01_architecture.md) | 中 |

第 1、2 项是前后端契约不一致造成的功能缺陷，改动小、收益高，建议在阶段 0 就修复。

## 与后端文档的对应

| 前端关注点 | 后端文档 |
|---|---|
| 事件归约 | [09_api/02 事件契约](../09_api/02_events_contract.md) |
| 错误处理 | [09_api/03 错误模型](../09_api/03_access_and_errors.md) |
| 令牌 | [09_api/03 §3](../09_api/03_access_and_errors.md) |
| 审批 | [02_runtime/04 审批](../02_runtime/04_approval.md) |
| 信任可视化 | [05_security/03 信任与注入](../05_security/03_trust_and_injection.md) |
| 上下文与记忆视图 | [03_context](../03_context/) |
| 调用检视 | [08_observability](../08_observability/) |

## 去掉 RAG 的影响

删除 RAG 面板、RAG 状态、RAG 类型与 API 模块；上下文抽屉与记忆抽屉中涉及检索切片的展示一并移除；`SidecarStatusPill` 只显示 shell 与 web 两项；系统提示中引导用户"先建立索引"的文案删除。
