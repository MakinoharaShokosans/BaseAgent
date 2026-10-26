# 端点参考

前缀 `/api/v1`。除健康检查与文档外，所有端点都经过 [接入层闸门](03_access_and_errors.md)。请求与响应均为 JSON，SSE 端点除外。错误体统一，见 [03](03_access_and_errors.md)。

> 标记说明：**已有** 为当前实现；**新增** 为新版需要补上的；**移除** 随 RAG 删除。

## 1. 工作区

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `POST /workspaces` | 创建工作区，按 `root_path` 幂等 | 已有，需加路径校验 |
| `GET /workspaces` | 列出全部工作区 | 已有 |
| `GET /workspaces/{id}` | 查询工作区 | 已有 |
| `PATCH /workspaces/{id}` | 更新名称与描述 | 已有 |
| `DELETE /workspaces/{id}` | **级联删除**工作区、会话、流水、记忆 | 已有 |
| `GET /workspaces/{id}/memory` | 读取工作区共享记忆 | 已有 |
| `POST /workspaces/{id}/memory/facts` | 上浮事实到工作区记忆（用户手动沉淀） | 已有 |
| `POST /workspaces/{id}/memory/failures` | 记录全局避坑 | 已有 |
| `DELETE /workspaces/{id}/memory/{kind}/{index}` | 删除一条记忆 | **新增**，见 [记忆 §5.2](../03_context/04_memory.md) |

### 创建工作区

```json
POST /workspaces
{ "name": "Aegis Core", "root_path": "/home/u/Projects/Aegis", "description": "", "workspace_id": null }
```

`root_path` 是**安全边界**：它决定工具的 cwd、文件接口的根、路径沙箱的根。当前实现不校验它。新版必须校验：

1. 路径存在且是目录；
2. 规范化（`resolve()`）后保存；
3. **拒绝根目录 `/` 与过于宽泛的路径**（用户主目录本身、`/etc`、`/usr` 等系统目录），否则文件接口和工具沙箱的"工作区内"约束形同虚设；
4. 同一路径只能有一个工作区，冲突返回 409。

## 2. 会话

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `POST /workspaces/{id}/sessions` | 创建会话 | 已有 |
| `GET /workspaces/{id}/sessions` | 列出会话 | 已有 |
| `GET /sessions/{id}` | 查询会话 | 已有 |
| `DELETE /sessions/{id}` | 删除会话及其流水与记忆 | 已有 |
| `GET /sessions/{id}/turns?limit&before_id` | 分页查询对话流水 | 已有 |
| `GET /sessions/{id}/memory` | 读取会话已压缩记忆 | 已有 |
| `GET /sessions/{id}/context` | 上下文检视：各层装配结果与 Token 估算 | 已有，见 [提示词装配 §6](../03_context/03_prompt_assembly.md) |
| `GET /sessions/{id}/tasks` | 列出会话下的任务 | **新增**，见下 |

**缺口：没有任务列表接口。** 注册表有按会话列任务的方法，但没有对外路由。页面刷新或服务重启后，前端无法找回此前的任务，只能靠本地记住 `task_id`。新增 `GET /sessions/{id}/tasks?limit&before`，数据来自持久化的任务表（检查点表加任务元数据），而不是进程内存。

## 3. 任务

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `POST /sessions/{id}/tasks` | 提交任务，异步执行，返回 202 | 已有 |
| `GET /tasks/{id}` | 状态快照 | 已有 |
| `POST /tasks/{id}/approve` | 批准越级操作 | 已有 |
| `POST /tasks/{id}/reject` | 拒绝越级操作 | 已有 |
| `POST /tasks/{id}/resume` | 从检查点续跑 | 已有 |
| `POST /tasks/{id}/cancel` | 取消 | 已有 |
| `GET /tasks/{id}/stream` | SSE 事件流 | 已有，契约见 [02](02_events_contract.md) |
| `GET /tasks/{id}/timeline?limit` | 时间线（消息的投影） | 已有 |
| `GET /tasks/{id}/trace` | 下载全量因果轨迹 NDJSON | 已有 |
| `GET /tasks/{id}/llm_calls` | 底层模型调用记录 | 已有，需改为按需拉取，见 [事件 §6](../08_observability/01_events.md) |
| `GET /tasks/{id}/spans` | span 树 | **新增**，见 [追踪](../08_observability/03_tracing.md) |
| `DELETE /tasks/{id}` | 删除任务及其轨迹、事件、产物 | **新增** |

### 提交任务

```json
POST /sessions/{id}/tasks
{ "task_goal": "…", "parent_task_id": null, "permission_level": null }
```

- 同时运行的任务数受 `max_concurrent_tasks` 限制（默认 1），超出返回 **429**。
- `permission_level` 可覆盖会话基线。**这是一个有权限含义的字段**：客户端可以直接把基线设成 `full_permissions` 以免审批。UI 选择这个值是正常用法；但没有令牌认证时任何本机调用者都能这样做，见 [03 §2](03_access_and_errors.md)。新版建议：设为 `full_permissions` 需要在请求里带一个显式的确认字段（`confirm_full_permissions: true`），UI 在用户确认弹窗后才发送。
- `task_goal` 需要长度上限（例如 20000 字符），当前没有。

### 状态快照 `TaskOut`

```
task_id, session_id, workspace_id, task_goal,
status: queued | running | waiting_for_approval | succeeded | failed | cancelled | terminated
permission_level,
approval_request: ApprovalRequestOut | null,
milestones[], current_milestone_idx,
step_count, total_tokens, consecutive_errors,
should_terminate, termination_reason,
created_at, started_at, finished_at,
artifact_count
```

状态含义与迁移见 [主循环 §4](../02_runtime/02_main_loop.md)。快照是**投影**，不是 `AgentState`。

### 审批

```json
POST /tasks/{id}/approve   { "approval_id": "appr_…", "decision": "once" | "always" }
POST /tasks/{id}/reject    { "approval_id": "appr_…", "reason": "…" }
```

- 当前 `approval_id` 可选；省略时任何待审批请求都会被放行或拒绝。新版必须**必填**，与待审批请求不一致返回 409，以防止错配和重复提交，见 [审批 §4](../02_runtime/04_approval.md)。
- 任务不在 `waiting_for_approval` 状态时返回 409。
- 响应：`{ task_id, status, message }`，后台恢复执行。

### 续跑

`terminated` 与 `failed` 可续跑；`succeeded`、`cancelled` 不可；`running` 或 `waiting_for_approval` 返回 409（后者要走审批）。

## 4. 产物

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `GET /tasks/{id}/artifacts` | 列出产物 | 已有 |
| `GET /tasks/{id}/artifacts/{artifact_id}` | 下载原文 | 已有 |

路径参数是产物 id，不是文件路径；服务端从任务的 `artifacts` 表查路径，并校验位于产物根目录之内。

## 5. 文件（工作区内）

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `GET /workspaces/{id}/files/tree` | 文件树（忽略构建噪音与隐藏项，深度 8） | 已有 |
| `GET /workspaces/{id}/files/content?path` | 读取，单文件上限 5 MB | 已有 |
| `PUT /workspaces/{id}/files/content` | 保存 | 已有，需改进 |

路径解析后必须位于工作区内，越界返回 422。`PUT` 的改进点：

- 内容大小上限（与 `write_file` 的 1 MB 对齐）；
- 原子写入；
- 与 Agent 并发写同一文件时的冲突处理：请求带 `base_hash`（读取时的内容哈希），不一致返回 409，避免用户编辑覆盖 Agent 的修改，或相反。

## 6. 系统

| 方法与路径 | 作用 | 状态 |
|---|---|---|
| `GET /system/fs/directories?path` | 浏览本地目录，供选择器使用 | 已有，**有风险**，见 [03 §3](03_access_and_errors.md) |
| `POST /system/fs/pick-directory` | 唤起宿主机原生目录选择对话框 | 已有，**有风险** |
| `GET /health` | 进程健康（免鉴权） | 已有 |
| `GET /health/dependencies` | sidecar 连通性 | 已有，去掉 `rag` 项 |

## 7. 自省

| 方法与路径 | 作用 |
|---|---|
| `GET /skills` | 技能列表，含来源、信任、告警、是否高权限 |
| `GET /tools` | 已注册工具及其 Schema |
| `GET /mcp/servers` | MCP 服务器状态、工具数、被拒工具与原因 |
| `GET /models` | 各档端点（**不含密钥**），新增能力声明与熔断状态 |

自省接口是只读的，用于让用户看见"系统此刻装配了什么"。注意它们暴露了配置结构，同样需要令牌保护。

## 8. 移除

整组 `/rag/*`：`GET /rag/health`、`POST /rag/ingest`、`POST /rag/retrieve`；`/health/dependencies` 里的 `rag` 项；`schemas.py` 里的 RAG 相关模型。

## 9. 约定

| 项 | 约定 |
|---|---|
| 版本 | 路径前缀 `/api/v1`；破坏性变更升 `/api/v2`，两版并行一段时间 |
| 时间 | 秒级 Unix 时间戳，浮点 |
| 分页 | 游标式：`limit` 加 `before_id`（或 `before`）；响应不带总数 |
| 幂等 | `POST /workspaces` 按路径幂等；审批、取消可重复调用，重复调用返回当前状态而非错误 |
| 追踪 | 请求头 `X-Trace-ID` 可选，缺省服务端生成，响应头回传，错误体里也带 |
| 异步 | 耗时操作返回 202，结果通过状态查询或 SSE 获得 |
| 大小 | 请求体大小上限（默认 2 MB），超出 413 |
| 编码 | UTF-8 |

## 10. 不变量

1. 对外 DTO 永远不是内部状态对象。
2. 路径类参数要么是相对路径并经工作区校验，要么是资源 id，不接受任意绝对路径（目录浏览除外，且受限，见 03）。
3. 破坏性操作（删除）返回确定的结果，并级联清理关联数据。
4. 同一语义的错误在所有端点返回同一个 `code`。
5. 响应不含密钥、不含原始网页正文。

## 11. 测试要点

- 每个端点的成功路径与 4xx 路径；校验错误统一为 400。
- 工作区路径校验：不存在、非目录、根目录、过宽路径、符号链接。
- 并发上限：第二个任务返回 429；完成后恢复。
- 审批：缺少或错配 `approval_id` 返回 409；重复提交幂等；任务不在等待状态返回 409。
- 续跑：各终态的允许与拒绝。
- 文件：路径穿越、符号链接、超大文件、`base_hash` 冲突。
- 级联删除：会话、工作区、任务删除后无残留。
- 契约：用 OpenAPI 快照测试，字段变更需显式更新快照。
