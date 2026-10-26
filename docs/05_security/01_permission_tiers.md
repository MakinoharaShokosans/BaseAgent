# 工作区权限分层（策略）

本文只回答一个问题：**一次工具调用，是否可以不经人工同意就执行？** 它是策略层，不负责"执行时怎样限制进程"，那是 [Shell 沙箱](02_shell_sandbox.md) 的事。审批的挂起与恢复协议见 [02_runtime/04_approval.md](../02_runtime/04_approval.md)。

## 1. 要解决的问题

- Agent 具备执行命令和写文件的能力，不能让它自行决定"什么操作是安全的"。
- 全部操作都要人工确认会让 Agent 无法使用；全部放行又等于把工作区和主机交给模型。
- 判定本身必须**可预测**：同样的命令，今天拦、明天放，用户无法建立信任，也无法写测试。

## 2. 三级权限

| 级别 | 自动执行的范围 | 典型操作 |
|---|---|---|
| `read_only` | 只读探测 | 查看文件、检索、读日志、加载技能、委派研究 |
| `workspace_write` | 工作区内的读写与本地构建 | 写文件、编译、跑测试、本地 `git add/commit`、`rm`/`mv`/`cp` |
| `full_permissions` | 影响工作区之外或不可逆的操作 | 网络外联、依赖安装、`git push`、提权、容器操作 |

级别是全序：`read_only < workspace_write < full_permissions`。调用所需级别**不高于**当前任务基线即可直接执行，否则称为**越级操作**，必须走人工审批。

任务基线由配置 `permissions.default_level` 给出，默认 `workspace_write`。任务启动时写入 `AgentState.permission_level`，全生命周期不变。基线不会因为单次审批而提升：审批只放行具体动作，不改变级别。

## 3. 判定

判定是一个**纯函数**：

```python
check_permission(current_level, tool_name, args, config) -> PermissionDecision
```

零 I/O、零模型调用、不依赖工具实现。它决定"要不要打断人去审批"，一旦掺入模型判断就会出现不可解释的行为。因此它必须能离线单测、可重复。

### 3.1 判定顺序

```
1. tool_name ∈ full_permission_tools     → full_permissions
2. tool_name ∈ workspace_write_tools     → workspace_write
3. tool_name ∈ read_only_tools           → read_only
4. tool_name ∈ bash_tools                → 按命令内容分类（§3.2）
5. 其余（未知工具）                       → unknown_tool_level（默认 full_permissions）
```

显式名单优先于命令分类，名单之外的未知工具走保守兜底。这是"默认保守，放宽需要显式配置"的落点：第三方 MCP 工具在被你写进 `read_only_tools` 之前，一律要审批。

### 3.2 `bash` 命令分类

命令文本依次匹配两组正则（忽略大小写）：

1. 命中 `full_permission_patterns` → `full_permissions`。动作类型：命令含 `git push`、`curl`、`wget`、`ssh`、`scp`、`nc`、`telnet` 之一标为 `network_egress`，否则标为 `global_env`。
2. 命中 `workspace_write_patterns` → `workspace_write`，动作类型 `workspace_write`。
3. 都不命中 → `read_only`，动作类型 `read`。

默认模式在 `config.toml` 的 `[permissions]` 段：

| 类别 | 示例 |
|---|---|
| 全权限 | `git push`、`curl`/`wget`、`ssh`/`scp`/`rsync`、`pip install`、`npm install`、`yarn/pnpm add`、`apt/yum/dnf install`、`sudo`、`docker`/`kubectl`/`helm` |
| 工作区写 | 输出重定向、`rm`/`mv`/`cp`/`mkdir`/`touch`/`ln`、`sed -i`、`chmod`/`chown`、本地 `git add/commit/checkout/merge/rebase/reset/stash/tag/clean`、`make`/`cmake`、编译器、`pytest`、`npm run/test`、`tee` |

非法正则会被跳过并记录错误，不会导致整个判定失败。

### 3.3 判定结论

```python
PermissionDecision(
    allowed: bool,            # 是否可直接执行
    required_level: str,
    current_level: str,
    action_type: str,         # read | workspace_write | network_egress | global_env | privileged | unknown
    action_summary: str,      # 给审批人的一句话，bash 取命令前 200 字
    reason: str,              # 越级时附"所需级别高于当前级别"
)
```

`action_type` 是稳定枚举值，前端据此映射图标和文案，不要随意改名。

## 4. 这套分类能防什么，防不了什么

分类基于**命令文本的正则匹配**。这是一个明确的取舍：它确定、可测、可解释，代价是它不是语义分析。必须清楚地写下它的边界：

| 能防 | 防不住 |
|---|---|
| 模型直接写出 `git push`、`curl …`、`sudo …` | 通过解释器间接执行：`python -c "import urllib…"`、`node -e …` |
| 常见的包管理器安装 | 通过脚本间接执行：先 `write_file` 写一个 `x.sh`，再 `bash x.sh`（见下） |
| 误操作类的删除和覆盖 | 命令替换、变量拼接、`eval`、编码绕过 |

**脚本间接执行是最重要的一条。** `write_file` 属于 `workspace_write`，写一个脚本不触发审批；随后 `bash x.sh` 的命令文本里没有任何危险模式，被判为 `read_only`。脚本内容里的 `curl` 就此绕过了闸门。

因此结论是：**权限分层是"防误操作"和"防显式越权"的闸门，不是对抗恶意输入的安全边界。** 真正的物理边界在 [Shell 沙箱](02_shell_sandbox.md)（进程组、资源上限、路径校验），以及部署层面的约束（只监听回环地址、不以高权限用户运行）。

### 4.1 缓解方向（待定）

- 对"执行工作区内脚本"（`bash x.sh`、`./x.sh`、`python x.py`）单独设一档：若该文件在**本任务内被 `write_file` 写过**，则升至 `full_permissions`，或在审批卡片里附脚本内容。
- 在 sidecar 层断网（网络命名空间），让 `curl` 之类即使绕过策略也无法外联。这是机制层的根本解法，成本较高。

## 5. 豁免：会话白名单

审批时选 `always` 会把动作签名加入**会话级白名单**，之后相同动作免审。

- 签名：`md5(json({tool, args}, sort_keys=True))[:16]`，即**工具名加完整参数**的哈希。只有完全相同的调用才命中。`git push origin main` 与 `git push origin dev` 是两个签名。
- 与死循环检测的指纹同构但用途不同：一个表示"重复了"，一个表示"批准过"。
- 白名单按会话隔离，由任务注册表持有，不入检查点。进程重启即清空，这是偏安全的设计。

> 注：16 位十六进制摘要约 64 位，碰撞概率可忽略，但签名使用 MD5 只是做指纹，不具备抗恶意构造的要求，因为输入由我们自己序列化，攻击者无法让两个不同调用产生同一签名而不改变参数。若日后担心，可换 `sha256` 并保持长度。

## 6. 任务授权窗口

叶子工具（尤其是子智能体）需要知道"父任务的权威权限级别"，但它们看不到 `AgentState`。直接让工具构造时记住级别是错的：任务恢复时工具会被重新构造，而检查点里的级别才是权威值，两者可能不一致。

解决办法是**推送式绑定**：`TaskAuthority` 是每任务一个的小对象，`tool_runner` 每次进入时把状态里的级别推给它，工具只读它。预算账本的 `sync_parent_usage` 用的是同一手法：父任务的运行期事实由父级推给叶子，叶子不回读状态。

## 7. 子智能体的权限

动态子智能体的权限在派发前一次性收窄，**只降不升**，派发后不可扩张：

- 子级权限级别不高于父级当前级别。
- 子级工具集是父级工具集的子集，且排除 `denied_tools`（`spawn_subagent`、`delegate_research`，防止套娃和模型嵌套循环）。
- 子级遇到越级操作**直接失败**，把失败观察值回给父级，不挂起、不审批。理由见 [审批 §7](../02_runtime/04_approval.md)。
- 子级每次调用都按父级权限重新判定，而不是继承一次性结论。

## 7.1 实现状态

`guardrails.permissions`：`PermissionPolicy` 是 `[permissions]` 的快照（纯函数不读配置对象），`check_permission(policy, level, tool, args)`。签名改用 sha256 的前 16 位（见 §5 的注）。`tool_runner` 不对**不存在的工具名**做判定（它不会被执行，不必打扰人）。已知防不住的命令有测试固定当前行为（§4）。子智能体的权限收窄尚未实现。

## 8. 不变量

1. 判定是纯函数，同一输入同一输出。
2. 未知工具默认要求最高级别。
3. 基线级别在任务内不变，审批不提升基线。
4. 子智能体的权限与工具集只会比父级更窄。
5. 权限判定只发生在 `tool_runner`，工具实现不得自行判定，避免规则散落。
6. 每次审批的结果写入轨迹，可追溯。

## 9. 配置项

均在 `config.toml` 的 `[permissions]` 段：

| 项 | 含义 |
|---|---|
| `default_level` | 新任务的基线 |
| `unknown_tool_level` | 未知工具的所需级别 |
| `read_only_tools` / `workspace_write_tools` / `full_permission_tools` | 显式工具名单 |
| `bash_tools` | 按命令内容分类的工具名，默认 `["bash"]` |
| `full_permission_patterns` / `workspace_write_patterns` | 命令分类正则，使用单引号字面量 |

去掉 RAG 后，`read_only_tools` 中删除 `rag_search` 与 `delegate_code_search`。

## 10. 测试要点

- 每个显式名单、`bash` 的三种分类、未知工具兜底各一组用例。
- 对抗用例：大小写变体、命令里夹带 `;`、`&&`、管道、`$(…)`、引号包裹的危险词、`git push` 前后有空白。把"已知防不住"的用例也写进去并标注，防止日后误以为已覆盖。
- 非法正则被跳过且不影响其余规则。
- 级别归一化：非法级别回落到默认值。
- 签名稳定性：参数顺序不同但内容相同得到同一签名；任一参数不同得到不同签名。

## 11. 待定

- §4.1 的"脚本间接执行"缓解，选哪种。
- 是否把命令分类的"解析"从正则升级为 shell 词法解析（如 `shlex`），以减少引号和拼接导致的误判与漏判。
- `always` 白名单是否按"命令前缀"而不是"完整参数"放行（便利与安全的取舍）。
