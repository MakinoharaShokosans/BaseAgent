# HTTP API 与事件契约

Agent HTTP API 是系统**唯一的用户入口**（`127.0.0.1:8000`）：工作区、会话、任务的管理，加上一条 SSE 事件流。业务逻辑一律下沉到运行时、记忆、工具，接入层不含编排决策。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [01_endpoints.md](01_endpoints.md) | 全部端点参考：工作区、会话、任务、产物、文件、系统、自省；新增与移除 | 已写初稿 |
| [02_events_contract.md](02_events_contract.md) | SSE 订阅与重连、事件信封、每种事件的载荷、客户端清单 | 已写初稿 |
| [03_access_and_errors.md](03_access_and_errors.md) | 威胁模型、Host/Origin 闸门、令牌认证设计、文件系统端点风险、错误模型 | 已写初稿 |

## 资源模型

```
工作区 Workspace ─┬─ 记忆 WorkspaceMemory
  (root_path)     │
                  └─ 会话 Session ─┬─ 流水 Turns、会话记忆
                                   └─ 任务 Task ─┬─ 事件流（SSE）
                                                 ├─ 时间线 / 轨迹 / 调用记录
                                                 ├─ 产物 Artifacts
                                                 └─ 审批 Approval（挂起时）
```

## 设计原则

1. **接入层不做编排。** 路由只做校验、转换、调用下沉的服务，再把结果投影成对外 DTO。
2. **对外 DTO 不等于内部状态。** `TaskOut` 是投影，不暴露消息对象或内部字段。
3. **异步任务，同步提交。** 提交任务立刻返回 202 和任务快照，进度靠 SSE 和快照查询。
4. **契约先于实现。** 事件与端点的字段变更需要同步本目录，破坏性变更升版本。
5. **默认拒绝。** 只监听回环，Host、Origin、令牌三道门；文件接口以工作区为边界。
6. **错误可程序化处理。** 稳定的 `code`，统一信封，`trace_id` 贯通。

## 与运行时的衔接

| API 动作 | 运行时对应 |
|---|---|
| 提交任务 | 任务注册表登记，后台协程运行主循环，见 [主循环](../02_runtime/02_main_loop.md) |
| 批准 / 拒绝 | 提交决策并 `resume`，见 [审批](../02_runtime/04_approval.md) |
| 续跑 | 从检查点重新进入，见 [检查点](../02_runtime/03_checkpoint_and_resume.md) |
| 取消 | 取消后台协程，状态停在最近的提交点 |
| SSE | 注册表的缓冲与事件表，见 [事件](../08_observability/01_events.md) |

## 现状发现的问题

| # | 问题 | 位置 | 级别 |
|---|---|---|---|
| 1 | **没有令牌认证。** 令牌函数是占位，根路径还声称有，任何本机进程可不带凭据提交任务、读写文件 | [03 §2](03_access_and_errors.md) | **高** |
| 2 | `GET /system/fs/directories` 可浏览主机任意目录；`pick-directory` 弹原生窗口阻塞线程 | [03 §4](03_access_and_errors.md) | 高 |
| 3 | 工作区 `root_path` 不校验，可设为 `/`，使所有"工作区内"约束失效 | [01 §1](01_endpoints.md) | 高 |
| 4 | `permission_level` 可由请求直接设为 `full_permissions`，无二次确认 | [01 §3](01_endpoints.md) | 中 |
| 5 | 审批的 `approval_id` 可选，省略则不校验匹配 | [01 §3](01_endpoints.md) | 中 |
| 6 | 没有任务列表接口，页面刷新或重启后无法找回任务；任务与事件只在内存 | [01 §2](01_endpoints.md) | 中 |
| 7 | 文件保存无大小上限、非原子，且与 Agent 并发写同一文件时会互相覆盖 | [01 §5](01_endpoints.md) | 中 |
| 8 | 慢消费者的事件被静默丢弃，客户端不知情 | [02 §1.2、§8](02_events_contract.md) | 中 |
| 9 | `llm.call` 事件内联完整提示词，单条体积巨大 | [02 §5](02_events_contract.md) | 中 |
| 10 | 输入长度、请求体大小没有上限 | [03 §5](03_access_and_errors.md) | 低 |

第 1 到第 3 项要在任何"对他人可用"的场景之前解决；即便单用户本机，也建议最先处理。

## 去掉 RAG 的影响

删除 `/rag/*` 整组端点与对应 DTO，`/health/dependencies` 去掉 `rag`，`code_search.*` 事件移除，前端的 RAG 面板同步删除。
