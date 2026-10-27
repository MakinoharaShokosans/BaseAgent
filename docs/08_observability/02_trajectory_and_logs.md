# 轨迹与日志

事件面向实时的人，**轨迹**和**日志**面向事后的人与程序：审计、排障、评测。

| | 事件 | 轨迹 | 日志 |
|---|---|---|---|
| 受众 | 前端用户，实时 | 审计、评测 harness | 开发者、运维 |
| 粒度 | 阶段与工具 | 每步的因果记录 | 每个模块的运行细节 |
| 存储 | 内存加事件表（新版） | `storage/traces/{task_id}.jsonl` | `storage/logs/baseagent.jsonl` |
| 稳定性 | 对外契约 | **数据契约**，评测直接消费 | 不保证 |

## 1. 轨迹

### 1.1 记录契约

每步一条 JSONL，追加写，立即 flush。进程被强杀时已完成的步骤不会丢失：审计与评测数据比性能更重要。

```json
{
  "task_id": "...", "seq": 12, "ts": 1760000000.0,
  "type": "node | tool | guard | final",
  "node": "tool_runner", "phase": "executing",
  "thought": "", "tool_name": "bash", "tool_args": {},
  "observation_summary": "", "artifact_path": "",
  "ok": true, "step_count": 3, "total_tokens": 4210, "consecutive_errors": 0
}
```

| `type` | 含义 |
|---|---|
| `node` | 阶段的决策与结论（规划、评估） |
| `tool` | 一次工具调用的结果 |
| `guard` | 护栏动作：预算熔断、死循环拦截、连续错误通知、审批决定、金丝雀熔断 |
| `final` | 任务收尾：终止原因与脱敏后的交付摘要 |

写入失败（磁盘满、权限）只告警，**不中断任务**。

### 1.2 现状的缺口

读代码时发现，轨迹没有完整记录"到底做了什么"：

| # | 缺口 | 影响 | 级别 |
|---|---|---|---|
| 1 | **`tool_args` 从未被写入。** 记录函数有这个参数，但工具阶段调用它时只传了工具名、观察值摘要和产物路径，没有传入参。结果是：轨迹里能看到"调用了 bash，结果是这样"，**看不到执行的是什么命令** | 审计的核心信息缺失。出了问题无法回答"它到底跑了什么" | **高** |
| 2 | **子智能体内部的调用没有进入轨迹。** 运行器没有收到记录器，只发事件，而事件不落盘 | 动态委派的所有内部动作（含它执行的命令、被拒绝的越级尝试）事后不可查 | 高 |
| 3 | 审批决定写入轨迹，但缺少"被审批的是什么命令"，因为入参缺失 | 同第 1 项 | 中 |
| 4 | 模型调用没有轨迹记录（只有规划与评估的阶段结论） | 无法离线统计各档的调用次数、延迟、重试 | 中 |
| 5 | 没有 `schema_version`，也没有区分父子关系的字段 | 字段变更会无声破坏评测 | 低 |
| 6 | 文件无大小上限，按任务一个文件，不清理 | 长期占用磁盘 | 低 |
| 7 | 每条记录各开关一次文件 | 高频时系统调用多；影响不大 | 低 |

### 1.3 新版契约

在现有字段上扩展，并保持向后兼容：

```json
{
  "v": 2,
  "task_id": "", "seq": 0, "ts": 0.0,
  "type": "node | tool | guard | llm | subagent | final",
  "span_id": "", "parent_span_id": "",
  "node": "", "phase": "", "depth": 0, "role": "",
  "tool_name": "", "tool_args": {}, "tool_args_redacted": true,
  "observation_summary": "", "artifact_id": "",
  "llm": {"tier": "", "endpoint": "", "model": "", "usage": {}, "duration_ms": 0, "attempts": 1},
  "ok": true, "error_class": "",
  "step_count": 0, "total_tokens": 0, "consecutive_errors": 0
}
```

要点：

1. **必须记录 `tool_args`**，经脱敏函数处理，并用 `tool_args_redacted` 标明是否发生过替换。命令也在其中。
2. **新增 `llm` 类型**：由网关写入，每次调用一条，含重试与降级。
3. **新增 `subagent` 类型与 `depth`、`role`**：子智能体内的每次工具调用、越级拒绝、结论摘要都写入，`parent_span_id` 指向发起它的 `spawn_subagent` 调用。
4. **`span_id` / `parent_span_id`**：为 [追踪](03_tracing.md) 提供关联键。
5. `artifact_path` 改为 `artifact_id`，避免把磁盘路径写进可能被分享的数据。
6. `v` 字段版本化，评测 harness 按版本解析。

### 1.4 脱敏

统一的脱敏函数，事件、轨迹、日志三处共用，只实现一次：

| 规则 | 处理 |
|---|---|
| 键名含 `key`、`token`、`secret`、`password`、`authorization`、`cookie` 的值 | 替换为 `[REDACTED]` |
| 值匹配已知密钥形态（`sk-…`、`ghp_…`、`AKIA…`、PEM 头） | 替换 |
| 命令里出现 `Authorization:` 请求头、`--password`、`-p <值>` 等 | 替换对应片段 |
| 金丝雀 token | 替换 |

脱敏是**尽力而为**，不是保证。轨迹文件应按"可能包含敏感信息"对待，权限设为 0600，不对外提供下载以外的暴露。

### 1.5 读取

- 时间线接口读取轨迹文件，校验路径位于轨迹根目录之内（防路径穿越）。
- 大文件分页读取，不整个读入内存返回。
- 评测 harness 读取同一份文件，所以契约变更要同步 `agent_bench`。

### 1.6 保留

按 [事件 §6.1](01_events.md) 的策略一并清理。任务被用户删除时，轨迹、事件、大块内容一起删除。

## 2. 日志

Loguru，**双 sink**：

| sink | 级别 | 格式 | 用途 |
|---|---|---|---|
| 控制台 | INFO | 彩色文本 | 开发时看 |
| 文件 `storage/logs/baseagent.jsonl` | DEBUG | JSON Lines，`serialize=True` | 离线解析、按 `task_id` 聚合 |

文件 sink 的设置：后台线程写入（`enqueue=True`，不让磁盘抖动阻塞事件循环）；20 MB 轮转，保留 10 份；`backtrace=True` 但 **`diagnose=False`**，不打印局部变量，避免密钥等敏感值进日志。`setup_logging` 幂等。

### 2.1 现状的缺口

1. **日志与任务没有关联。** 绝大多数日志语句直接写文本，没有绑定 `task_id`、`session_id`、`span_id`。排障时无法从一个任务过滤出它的全部日志。只有 sidecar 的少数位置做了 `logger.bind`。
2. **Sidecar 的日志与 Agent 的日志是两条独立的流。** 没有共同的请求 ID，一次 `bash` 调用在两边的日志无法串起来。
3. **没有脱敏。** 日志里可能出现工具入参、命令、LLM 错误体。
4. **以中文自由文本为主。** 对人友好，对程序不友好；评测脚本只能解析少数结构化字段。

### 2.2 新版设计

1. **上下文绑定：** 用 `contextvars` 保存 `task_id`、`session_id`、`span_id`、`step`，通过 Loguru 的 `patcher` 自动注入每条日志。不需要每个调用点手动 `bind`。任务开始时设置，子任务和子智能体里自动继承。
2. **跨进程关联：** Agent 调用 sidecar 时带请求头 `X-Request-ID`（取 `span_id`），sidecar 记录到每条日志。`ServiceClient` 已有 `trace_id` 字段，接上即可。
3. **统一脱敏：** 在 `patcher` 里对 `message` 和 `extra` 过一遍脱敏函数。
4. **关键事件结构化：** 对少数评测要用的日志（预算熔断、重试、降级、审批、金丝雀）使用固定的 `event` 字段名和 `extra` 键，其余保持自由文本。
5. **级别约定：**

| 级别 | 用途 |
|---|---|
| DEBUG | 细节、载荷尺寸，默认只写文件 |
| INFO | 生命周期：任务开始与结束、阶段切换、连接建立 |
| WARNING | 降级、重试、被拒绝的操作 |
| ERROR | 失败但任务可继续 |
| CRITICAL | 安全熔断（金丝雀） |

6. 日志目录权限 0700，文件 0600。

## 3. 三者的关系

```
一次工具调用
 ├─ 事件       tool.call → tool.result          （给用户看）
 ├─ 轨迹       type=tool, tool_args, summary     （给审计与评测）
 └─ 日志       DEBUG 细节，带 task_id/span_id     （给开发者）
```

同一个事实在三处各有各的粒度，**不要让它们互相依赖**：事件发不出去，轨迹照写；轨迹写不进去，日志照打；日志挂了，任务照跑。三者全是旁路。

## 4. 不变量

1. 轨迹每条记录立即落盘，强杀不丢已完成步骤。
2. 观测写入失败永不中断任务。
3. 轨迹必须包含工具入参（脱敏后）。
4. 子智能体的内部动作可追溯到发起它的调用。
5. 事件、轨迹、日志使用同一个脱敏函数。
6. 日志不打印局部变量。
7. 轨迹读取接口不能逃出轨迹根目录。

## 5. 测试要点

- 轨迹：每种 `type` 的记录齐全；`tool_args` 存在且已脱敏；子智能体调用带 `depth` 与 `parent_span_id`；写入失败不抛异常；并发写入同一文件不交错。
- 脱敏：键名规则、值形态规则、命令片段、金丝雀、嵌套结构。
- 日志：上下文自动注入；子任务继承；sidecar 日志带同一个请求 ID；`diagnose=False`。
- 读取：路径穿越被拒；大文件分页。
- 契约：用 `v` 区分版本，旧版本文件仍可被评测解析。

## 6. 待定

- 轨迹是否迁移到 SQLite 以便查询。JSONL 对追加和评测友好，对"找出所有被拒绝的命令"这类查询不友好。折中：保留 JSONL 为真源，另外异步导入一张索引表。
- 是否对轨迹做签名或哈希链，防止事后篡改。单用户本机场景下优先级低。
- 日志是否需要远程汇聚。目前明确不做。

## 7. 实现状态

- **轨迹**（`observability.TrajectoryRecorder`）：作为事件中心的旁路 sink，每个事件发布后同步追加一行到 `{traces_dir}/{task_id}.jsonl`（0600，立即 flush）。记录由事件推出，所以事件、轨迹用的是**同一次脱敏**（§1.2 第 1 项已修：`tool_args` 存在，`tool_args_redacted` 标明是否发生过替换）。类型 `node | tool | guard | llm | subagent | final`，带 `v=2`。读取按行分页，强杀留下的撕裂最后一行被跳过；`task_id` 只接受 `[A-Za-z0-9_-]{1,64}`，读取不能逃出根目录；随任务删除一起删。`GET /tasks/{id}/trace` 优先下载这个文件。
- **日志**（`observability.setup_logging`）：控制台与 JSONL 文件双 sink，`enqueue=True`、`diagnose=False`、轮转与保留份数取自配置，目录 0700、文件 0600；`patcher` 把当前任务的 `task_id` 自动注入每条日志（§2.2 第 1 项），并对消息和额外字段过统一脱敏（额外字段按键名，如 `password=`）；`setup_logging` 幂等。
- **未做**：`span_id` / `parent_span_id`（追踪，见 [追踪](03_tracing.md)）、日志与 sidecar 的 `X-Request-ID` 关联（§2.2 第 2 项）、轨迹的大小上限与保留清理（§1.2 第 6 项）、`agent_bench` 评测对轨迹的消费。
