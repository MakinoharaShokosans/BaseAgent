# BaseAgent

一个自研的 Agent 运行时底座，给想自己做 Agent、又不想被现成框架绑住的开发者，当作 vibe coding 的起点。

> 项目原名 Aegis。命令行、环境变量、数据目录等已经全部改成 BaseAgent；旧名字仍然兼容（`aegis` 命令、`AEGIS_*` 变量、`~/.local/share/aegis` 数据目录），不需要你手动迁移，细节见 [配置 §9](docs/01_architecture/04_configuration.md)。

## 为什么有这个项目

我想做的是"能放进自己产品里的 Agent"，找下来发现两头都不太合适：

- **工作流编排类框架（LangGraph 之类）**：擅长把流程画成图。但 Agent 的难点不在"画图"，而在审批挂起与续跑、预算与终止、上下文越来越长之后怎么办、工具输出里的注入怎么防。这些往往要自己在框架外面补，补完发现框架只剩一层胶水。
- **Claude Code 一类成品的重型 Agent**：能力强，但提示词、工具和上下文都偏大，每一步的 Token 消耗高，也很难拆开改成自己的垂直场景。

BaseAgent 想走中间：**自己掌握主循环，把这些共性问题做成可读、可测、可改的代码**，让你在上面用 vibe coding 的方式（让 AI 编码助手读文档、改代码、跑测试）快速长出自己的 Agent。

它适合：
- 想从零做自己的 Agent，希望主循环、状态、工具、权限都是看得见摸得着的代码；
- 对 Token 成本敏感，愿意自己调规划和验收的策略；
- 会用 AI 编码助手协作开发，需要一份写得清楚的设计文档和一套能兜底的测试。

它不适合：
- 想要开箱即用的产品，或者拖拽式的流程编排；
- 需要对抗恶意代码的强隔离（见下面的安全提示）；
- 需要很多现成的集成。目前内置能力很少，靠 MCP 和自己扩展。

## 它是什么，不是什么

**是**：一个可以直接跑起来的最小完整 Agent，包括运行时、HTTP API、事件流、两个独立的 sidecar 服务、评测工具，以及一个用来观察运行过程的控制台。它默认的用法是"给一个工作区和目标，让 Agent 写代码"，但这只是第一个场景，不是边界。

**不是**：不依赖 LangGraph / LangChain，也不含 RAG。前端控制台是调试和观察用的，不是产品界面。

## 设计取向

- **主循环只有一个动作**：调模型；有工具调用就执行并把结果追加进 transcript，再调模型；没有工具调用即结束（[决策 0006](docs/12_decisions/0006-pure-agent-loop.md)）。没有规划、验收这类阶段，也没有预算和步数上限，控制权在模型手里，代码只管护栏：权限与审批、重复调用拦截、金丝雀、检查点。
- **做事的方式写在提示词里，不写在代码里。** 先确认需求，多步任务用 `update_plan` 拆成子任务，一个做完再推进下一个，每一步调用工具前用一两句话汇报“已完成什么、下一步做什么、子任务进度”（[工作方式](docs/02_runtime/07_working_method.md)）。
- **做不到要能体面地停。** 被拒绝、缺信息时，任务带着已完成的部分结束，说明卡在哪，而不是反复重试。进程崩了、等审批等了一晚上，都能从上一个检查点续跑。
- **上下文有治理，长任务也能跑下去。** 读取结果不会被截成头尾；旧的读取结果折成一行存根（需要时用同样的参数重读）；仍然太大时，较早的步骤由快速档模型总结成摘要，原始记录不动；模型报上下文溢出时自动压缩后重试一次。会话级另有摘要压缩，也可以手动触发。
- **安全按"外部内容是数据，不是指令"设计。** 网页和 MCP 返回的内容走不可信信封；外部信息只能经过隔离的websearch 工具进来；高风险操作按权限级别要求人工审批；提示词里埋了金丝雀，泄露就终止。
- **所有模型都走一个网关**，分"推理 / 快速"两档，各档有按顺序降级的端点列表。当前配置用的是 DeepSeek（OpenAI 兼容协议）。

## 目前的状态

诚实地说：

- **做完并有测试覆盖**：运行时、检查点与审批、记忆与压缩、内置工具、`bash` 和 `web_search` 两个 sidecar、子智能体（研究与动态委派）、MCP 接入、HTTP API 与 SSE、评测框架。后端约 2000 个测试，另有前端测试，`import-linter` 约束层间依赖。
- **没有当前架构的真实模型数据。** 评测框架和 11 个内置用例都在，但唯一一轮真实基线是改成纯 agent loop 之前跑的，已经不能代表现在的行为，也已删除。用 `baseagent-bench` 自己量一轮。
- **没有做过横向对比。** 我没有拿它和别的框架比过成功率或 Token 消耗，所以"省 Token"只是设计取向，不是测出来的结论。想知道在你的任务上表现怎样，用 `baseagent-bench` 自己量。
- **只验证了 DeepSeek。** 别的 OpenAI 兼容端点理论上改配置就能用，但没验证过。

## 在它上面做自己的 Agent

这些是你最可能动的地方，都是普通代码和文本：

| 想改什么 | 去哪 |
|---|---|
| 做事的方式（确认、规划、汇报、长任务上下文） | `backend/src/agent_runtime/prompts/system.md` |
| 子智能体、压缩、联网搜索的提示词 | `backend/src/agent_runtime/prompts/*.md` |
| 加自己的工具 | 继承 `AgentTool`，在 `runtime/factory.py` 的 `build` 里加进工具表。**目前没有插件式入口，需要改这一处** |
| 接现成的外部工具 | 在 `config/config.toml` 的 `[mcp.servers.*]` 配置，每个服务器默认关闭 |
| 可复用的做事流程 | `backend/src/skills/<名字>/SKILL.md`，也支持全局和工作区里的技能 |
| 权限、上下文阈值、工具参数 | `config/config.toml`（`[runtime.*]`、`[context.*]`、`[tools.*]`） |
| 换模型或加降级端点 | `config/config.toml` 的 `[models.*]` |

用 AI 编码助手协作时，几个实用的做法：

- 先让它读 [`docs/`](docs/README.md)。文档描述的是代码的真实行为，是改动的"合约"；和代码不一致时以代码为准并修正文档。
- 改完跑 `make check`（lint、类型检查、依赖方向、测试）。这套测试是兜底的，改主循环或权限相关的代码尤其要靠它。
- 改了提示词或上下文策略，用 `baseagent-bench` 前后各跑一次再 `--compare`，别凭感觉。

## 快速开始

需要 `uv`、Node 与 `pnpm`。解释器版本由 `backend/.python-version` 指定（3.12），`uv` 会按需获取。

```bash
cp .env.example .env     # 填写 DEEPSEEK_API_KEY
make install             # 安装依赖，需要联网
make check               # lint + 类型检查 + 离线测试
```

`make help` 列出全部任务。

启动（沙箱 sidecar、后端、前端一起拉起，Ctrl+C 一起收掉）：

```bash
cd backend && uv run baseagent start      # 控制台在 http://127.0.0.1:5173，后端在 11328
uv run baseagent token                     # 显示访问令牌文件的路径（令牌不会被打印）
```

前端通过同源代理访问 API，令牌由代理在服务端侧注入，浏览器代码里没有它。后端端口在 `config/config.toml` 的 `[server] port` 配置。

评测（会花 Token，默认不运行；服务需要已启动）：

```bash
cd backend && uv run baseagent-bench --list              # 内置用例
uv run baseagent-bench --repeat 3 --label baseline       # 跑并生成 eval_reports/*.md|json
uv run baseagent-bench --compare eval_reports/a.json eval_reports/b.json
```

## 目录

```
backend/     Python 后端：运行时、API、sidecar 服务、MCP、评测
frontend/    React 前端控制台
config/      config.toml（可入库，不含密钥）
docs/        设计文档
.env.example 环境变量示例，复制为 .env 后填写
```

## 文档

从 [`docs/README.md`](docs/README.md) 开始。几个入口：

| 想了解 | 看 |
|---|---|
| 项目是什么、范围、术语 | [`docs/00_overview`](docs/00_overview/) |
| 整体架构与实现路线 | [`docs/01_architecture`](docs/01_architecture/) |
| 主循环、状态、审批 | [`docs/02_runtime`](docs/02_runtime/) |
| 工具、MCP、技能 | [`docs/04_tools`](docs/04_tools/) |
| 配置 | [`docs/01_architecture/04_configuration.md`](docs/01_architecture/04_configuration.md) |
| 评测 | [`docs/11_testing/01_strategy.md`](docs/11_testing/01_strategy.md) |
| 为什么这么定 | [`docs/12_decisions`](docs/12_decisions/) |

## 接下来

会持续优化，目前明确还没做的：

- 会话压缩改为异步；
- 任务内压缩在真实长任务上的触发线与摘要质量还没调过；
- 把验收通过的做法沉淀成工作区经验；
- 工具的插件式注册入口，让加工具不用改装配代码；
- 更多、更难的评测用例，以及在不同模型上的数据；
- 前端的生产形态托管（现在只有开发服务器）。

## 安全提示

- 服务只监听回环地址（`127.0.0.1`），配置里改成其他地址会拒绝启动。
- Agent 具备执行命令和写文件的能力。当前沙箱是"防失控"的资源限制，**不是**对抗恶意代码的隔离环境，详见 [`docs/05_security`](docs/05_security/)。
- 第三方 MCP 服务器（stdio 类型）本质上是在本机运行的任意程序，所以默认全部关闭，开启前请确认来源。
- `.env`、`storage/`、令牌文件都已在 `.gitignore` 中，请勿提交。
