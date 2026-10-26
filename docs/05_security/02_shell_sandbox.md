# Shell 沙箱（机制）

本文描述命令**怎样被执行并被限制**，即 `bash_shell` 子系统。"该不该执行"是策略，见 [权限分层](01_permission_tiers.md)。两者分工明确：

| | 权限分层 | Shell 沙箱 |
|---|---|---|
| 位置 | Agent 进程内，`tool_runner` | 独立进程，`:8002` |
| 回答 | 要不要人工同意 | 执行时能做什么、用多少资源 |
| 手段 | 命令文本分类 | 审计、进程组、资源上限、超时硬杀、配额排队 |
| 失败表现 | 挂起等待审批 | 拒绝 / 超时 / 排队 |

即使审批通过，沙箱的限制依然生效。

## 1. 为什么是独立进程

执行任意命令的能力不应与编排逻辑共享地址空间：

- 命令跑崩、被 OOM、卡死，都只影响 sidecar，不影响 Agent 的状态和检查点。
- 安全边界只存在一处。`BashTool`（Agent 侧）不做任何沙箱判断，只组装参数、转译结果。这样不会出现"某个调用方绕过了检查"的路径。
- 只监听回环地址（`127.0.0.1` / `localhost` / `::1`）。配置里写其他地址会在加载时被拒绝，这是写死的安全红线。

## 2. 对外接口

| 端点 | 作用 |
|---|---|
| `POST /api/v1/shell/execute` | 执行命令 |
| `GET /api/v1/health` | 存活与配额快照 |

请求字段：`workspace_id`、`workspace_root`、`task_id`、`step_id`、`command`、`timeout_sec`（可选）。

错误映射：

| 状态码 | 含义 |
|---|---|
| 400 | 请求体校验失败 |
| 403 | 命令被审计拒绝（`AUDIT_REJECTED`），请求合法但被安全策略拒绝 |
| 422 | 写入目标越界（`PATH_ESCAPE_DETECTED`）或工作区非法（`WORKSPACE_INVALID`） |

响应契约（`extra="forbid"`，多余字段直接报错）：

| 字段 | 含义 |
|---|---|
| `status` | `SUCCESS` / `FAILED` / `TIMEOUT` / `KILLED` / `QUEUED` |
| `exit_code` | 退出码；被信号终止为负值（如 `-9`）；`QUEUED` 时为空 |
| `distilled_stdout` / `distilled_stderr` | 蒸馏后的输出 |
| `is_truncated` | 任一流是否被截断 |
| `artifact_path` | 全量日志的落盘路径 |
| `execution_time_ms` | 实际耗时 |
| `timed_out` | 是否因超时被硬杀 |
| `queue_wait_sec` / `estimated_wait_sec` | 排队等待与预估 |

## 3. 执行流水线

```
命令 ──► ① 前置审计 ──► ② 写入目标越界校验 ──► ③ 申请内存配额 ──► ④ 沙箱执行 ──► ⑤ 落盘与蒸馏 ──► 释放配额
          403            422                    QUEUED               TIMEOUT/KILLED
```

任一阶段拒绝，后面的阶段都不会发生。配额在 `finally` 中释放，无论成功、超时还是异常。

### 3.1 前置审计（命令黑名单）

纯正则、零模型、无 I/O。按顺序匹配，先命中先拒绝：

| 规则 ID | 拦截 |
|---|---|
| `RM_RF_ROOT` | 递归强制删除根目录 |
| `MKFS` | 格式化文件系统 |
| `DD_TO_DEVICE` | `dd of=/dev/*` 写裸设备 |
| `POWER_CONTROL` | `shutdown` / `reboot` 等 |
| `FORK_BOMB` | fork 炸弹 |
| `SENSITIVE_SYSTEM_WRITE` | 对 `/etc/passwd`、`/etc/shadow`、`/etc/sudoers`、`/boot`、`/sys`、`/proc/sys`、块设备等的写操作 |
| `RECURSIVE_ROOT_PERMISSION` | 对根目录递归改权限 |
| `CUSTOM_DENY` | 配置追加的自定义规则 |

空命令直接拒绝（`EMPTY_COMMAND`）。拒绝时返回规则 ID、原因、命中片段（截断到 200 字符），用于模型自我修正和审计。

### 3.2 写入目标越界校验

从命令中提取**显式**写入目标：输出重定向、`tee`、`dd of=`，再加上调用方声明的写入路径。逐个 `Path.resolve()`（展开符号链接和 `..`）后要求位于工作区根的子树内，否则 `PathEscapeError`。`/dev/null`、`/dev/stdout` 等安全伪设备放行；以 `&` 或 `$` 开头的目标（文件描述符、变量）跳过。

### 3.3 内存配额

全局内存池 `GlobalMemoryBudget` 做并发治理：

- **内存配额**：总量 `memory_pool_mb`；每次申请量取调用方预估，缺省取 `rlimit_as_mb`。不足时**等待而不是失败**，任务结束后释放并唤醒下一个。
- **并发上限**：`max_concurrent` 个槽位，与内存配额解耦。
- **排队预估**：按"内存缺口比例 × 近期平均耗时 × 排队深度"估算等待秒数。
- **0 等待探测**：`queue_wait_sec <= 0` 时池满立即返回 `QUEUED`，附带预估等待，调用方据此退避。

`BashTool` 收到 `QUEUED` 时回给模型一句"当前并发配额已满，请稍后重试或减少并行命令"，并标记 `ok=True`，不计入连续错误。

### 3.4 沙箱执行

用 `asyncio.create_subprocess_shell` 启动，在 `preexec_fn` 里依次做：

| 手段 | 作用 |
|---|---|
| `os.setsid()` | 独立会话与进程组，便于整组清退孤儿进程 |
| `RLIMIT_AS` | 虚拟内存上限 |
| `RLIMIT_FSIZE` | 单文件大小上限 |
| `RLIMIT_CPU` | 纯 CPU 时间上限 |

其余约束：

- `cwd` 绑定**目标工程根目录**（不是临时目录）。
- `stdin` 接 `DEVNULL`，命令无法等待交互输入而卡死。
- stdout 与 stderr 各由独立协程流式读取，分块大小 `stream_chunk_bytes`。

**超时：两段式硬杀。** 到点先对整个进程组发 `SIGTERM`，等待 `sigterm_grace_sec`，仍未退出则升级 `SIGKILL`。因为是按进程组发信号，命令派生的子进程一并清退。

### 3.5 落盘与蒸馏

完整输出**只落盘，不驻留内存**：`{artifacts_dir}/{task_id}/step_{step_id}_bash.log`。`task_id` 必须是单层安全目录名（白名单校验），含路径分隔符会被拒绝，防止通过 `task_id` 逃出落盘根目录。

内存中每个流只保留有界切片：头部行、尾部行（双端队列）、关键字命中行、结构化嗅探缓冲。蒸馏策略按优先级：

1. 流完整且能解析为 JSON，或解析为映射/序列的 YAML → **原样返回**，避免截断破坏语法。纯文本不会被误判为 YAML，因为要求结果是映射或序列。
2. 失败态（退出码非零）→ 按关键字行过滤（`error:`、`fatal:`、`warning:`、`undefined reference`、`Segmentation fault`、`Traceback`），无命中则退回尾部。
3. 成功态 → 头部 `distill_head_lines` 行加尾部 `distill_tail_lines` 行，中间附省略提示和日志路径。

嗅探缓冲超过 `structured_sniff_max_bytes` 就丢弃，大输出不会因为想"看看是不是 JSON"而占住内存。

## 4. 沙箱能做到什么，做不到什么

必须写清楚，避免对它的能力有误判：

| 威胁 | 现状 |
|---|---|
| 命令卡死、跑飞、占满 CPU 或内存 | **能防**：硬超时、进程组清退、`RLIMIT_CPU` / `RLIMIT_AS` |
| 写出超大文件 | **能防**：`RLIMIT_FSIZE` |
| 破坏性单条命令（`rm -rf /`、`mkfs`、`dd` 写盘） | **能防**：黑名单 |
| 通过重定向、`tee` 写到工作区之外 | **能防**：显式写入目标越界校验 |
| 并发命令挤爆内存 | **能防**：内存池排队 |
| 通过解释器写工作区之外：`python -c "open('/home/x','w')…"`、`sed -i`、`cp`、`mv` 到外部路径 | **防不住**：越界校验只看重定向、`tee`、`dd of=`，不解析其他命令的写入目标 |
| **读取**工作区之外的任何文件（`cat ~/.ssh/id_rsa`） | **防不住**：越界校验只管写，读不受限 |
| 网络外联 | **防不住**：没有网络隔离，只有权限分层的正则这一道软闸门 |
| 子进程数量（fork 炸弹的变体、大量并发子进程） | **防不住**：没有 `RLIMIT_NPROC`；`RLIMIT_AS` 与 `RLIMIT_CPU` 是**每进程**而不是整棵进程树的总量 |
| **读取环境变量中的密钥** | 见 §5.1 |

结论：这是一个**防失控、防误操作**的执行笼子，不是能抵御恶意代码的隔离环境。它不能替代容器或用户命名空间。威胁模型与部署假设（单用户、本机、回环）要在 [安全总览](README.md) 里写明；如果将来要面对不可信的任务来源，必须升级到真正的隔离。

## 5. 已发现的问题

读代码时发现两处需要在实现新版时修复，写在这里以免遗忘。

### 5.1 子进程继承了完整环境变量（高）

`create_subprocess_shell` 没有传 `env=`，子进程继承 sidecar 的环境。sidecar 由 Agent 的监督器用 `os.environ.copy()` 拉起，而配置层会用 `load_dotenv` 把 `.env` 载入进程环境。若调用顺序如此（需实现时核实），则 `TERRA_KEY`、`LUNA_KEY`、`GITHUB_TOKEN` 等密钥会出现在命令的环境里，模型执行 `env` 或 `printenv` 即可读到，读到后还可以通过 `curl` 外传。

修复：执行时显式构造最小环境。

```python
env = {
    "PATH": safe_path,
    "HOME": task_scratch_dir,      # 不用真实 HOME，避免读到用户配置
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}
```

同时让监督器给 sidecar 传白名单环境，而不是整份复制。两道防线都要，只修一处不够。

### 5.2 审计与越界拒绝的原因没有到达模型（中）

沙箱对违规返回 403 或 422，响应体里有 `rule`、`reason` 等结构化字段。`ServiceClient` 把非 200/201/202 的状态码统一抛成 `DependencyUnavailableError`，原因放在 `context["body"]` 里；`BashTool` 回给模型的是 `str(exc)`，按我读到的代码只含"sidecar 返回异常状态码: 403"，不含规则和原因（实现时核实 `AgentError.__str__` 是否带上下文）。

后果：模型不知道为什么被拒，更难自我修正，也容易重试同一条命令，直到被死循环检测拦下。

修复：`BashTool` 对 403 与 422 单独处理，把 `reason` 和 `rule` 写进失败观察值；`ServiceClient` 允许调用方声明"这些状态码是业务拒绝，不是依赖不可用"，不把它们混成同一种异常。这一点也会影响 sidecar 真正宕机时的提示。

## 6. 配置项

`config.toml` 的 `[bash_shell]` 段：

| 项 | 默认 | 含义 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8002` | 只允许回环地址 |
| `timeout_sec` | 60 | 单命令默认硬超时 |
| `sigterm_grace_sec` | 2 | `SIGTERM` 到 `SIGKILL` 的宽限期 |
| `rlimit_as_mb` | 2048 | 每进程虚拟内存上限 |
| `rlimit_fsize_mb` | 50 | 单文件大小上限 |
| `rlimit_cpu_sec` | 300 | 每进程 CPU 时间上限 |
| `memory_pool_mb` | 8192 | 全局内存池总量 |
| `max_concurrent` | 4 | 并发执行上限 |
| `artifacts_dir` | `storage/artifacts` | 全量日志根目录 |
| `stream_chunk_bytes` | 65536 | 管道分块大小 |
| `distill_head_lines` / `distill_tail_lines` | 20 / 30 | 成功态保留行数 |
| `distill_max_keyword_lines` | 200 | 失败态关键字行上限 |
| `structured_sniff_max_bytes` | 262144 | 结构化嗅探缓冲上限 |

`RLIMIT_AS` 限制的是虚拟地址空间，JVM、部分 Node 和带大映射的程序可能在远低于物理内存的实际用量时就启动失败。遇到这类工具时需要调高该值，或者对其单独处理。

## 7. 不变量

1. 沙箱的所有拒绝和限制都在 sidecar 内完成，Agent 侧不重复实现。
2. 任何命令都在独立进程组中运行，超时后整组清退。
3. 完整输出只落盘，内存占用与输出大小无关。
4. 落盘路径只能落在 `artifacts_dir/{安全 task_id}/` 下。
5. 配额无论成败都会释放。
6. 服务只监听回环地址。

## 8. 测试要点

- 审计：每条规则的正例和反例；规避写法（引号、反斜杠、`;` 前后空白）；空命令。
- 越界：`../`、绝对路径、符号链接指向外部的重定向目标；`/dev/null` 放行；`$VAR` 目标跳过。
- 执行：超时触发两段式硬杀；命令派生的子进程被一并清退；退出码、信号终止的负值。
- 资源：超过 `RLIMIT_FSIZE` 的写入被截断；`RLIMIT_CPU` 触发终止。
- 配额：池满 0 等待返回 `QUEUED`；排队后被唤醒；异常路径也释放配额。
- 蒸馏：完整 JSON 原样返回；截断的 JSON 走文本蒸馏；纯文本不被误判为 YAML；超大输出不撑内存。
- `task_id`：含 `/`、`..` 被拒。
- §5 两个问题各加回归测试：`env` 输出中不含任何密钥；被拒绝的命令，其原因出现在模型看到的观察值里。

## 9. 待定

- 是否引入 `RLIMIT_NPROC` 以及用 cgroup 限制整棵进程树。
- 是否在 sidecar 中使用网络命名空间或 `unshare` 断网，从根上解决外联。
- 读取越界是否纳入越界校验（对 `cat`、`head`、`grep` 等常见只读命令的路径参数做检查），以及这会带来多少误杀。
- 沙箱是否升级为容器或 `bubblewrap` 一类的真正隔离，以及触发这一升级的条件。
