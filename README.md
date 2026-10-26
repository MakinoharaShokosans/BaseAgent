# Aegis

本机运行的代码工程 Agent。在浏览器控制台里给定工作区和目标，Agent 自主规划、调用工具（读写文件、执行命令、联网研究、委派子智能体），在受控的权限与预算内完成任务，并实时呈现全过程。

自研 Agent Runtime：**不依赖 LangGraph / LangChain，不含 RAG**。

> **当前状态：骨架阶段。** 设计文档已完成并有 5 条已采纳的决策；目录结构、依赖声明、依赖方向检查、参考配置已就位；业务代码尚未开始。

## 文档

从 [`docs/README.md`](docs/README.md) 开始。几个入口：

| 想了解 | 看 |
|---|---|
| 项目是什么、范围、术语 | [`docs/00_overview`](docs/00_overview/) |
| 整体架构与实现路线 | [`docs/01_architecture`](docs/01_architecture/) |
| 技术选型 | [`docs/01_architecture/05_tech_stack.md`](docs/01_architecture/05_tech_stack.md) |
| 配置 | [`docs/01_architecture/04_configuration.md`](docs/01_architecture/04_configuration.md) |
| 为什么这么定 | [`docs/12_decisions`](docs/12_decisions/) |

## 目录

```
backend/     Python 后端：运行时、API、sidecar 服务
frontend/    React 前端控制台
config/      config.toml（可入库，不含密钥）
docs/        设计文档
.env.example 环境变量示例，复制为 .env 后填写
```

## 快速开始

需要 `uv`、Node 与 `pnpm`。解释器版本由 `backend/.python-version` 指定（3.12），`uv` 会按需获取。

```bash
cp .env.example .env     # 填写 DEEPSEEK_API_KEY
make install             # 安装依赖，需要联网
make check               # lint + 类型检查 + 离线测试
```

`make help` 列出全部任务。

## 安全提示

- 服务只监听回环地址（`127.0.0.1`），配置里改成其他地址会拒绝启动。
- Agent 具备执行命令和写文件的能力，当前沙箱是“防失控”的资源限制，**不是**对抗恶意代码的隔离环境，详见 [`docs/05_security`](docs/05_security/)。
- `.env`、`storage/`、令牌文件都已在 `.gitignore` 中，请勿提交。
