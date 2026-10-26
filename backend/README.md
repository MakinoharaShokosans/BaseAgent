# Aegis 后端

Agent 运行时、HTTP API 与两个 sidecar（`bash_shell`、`web_search`）。设计见仓库根的 [`docs/`](../docs/)。

当前状态：**骨架**。目录结构、依赖声明、模块依赖方向的检查、配置文件的一致性测试已就位，业务代码尚未开始。

## 目录

```
src/
├── agent_runtime/     运行时：core / loop / checkpoint / approval / llm / context /
│                      memory / guardrails / subagents / skills / observability / api
├── tools/             工具层：core（注册表、派发器、客户端）、builtin（内置工具）
├── mcps/              MCP 接入
├── services/          独立进程：bash_shell、web_search
├── skills/            内置技能包
└── evaluation/        评测框架
tests/
```

依赖只能从上往下，规则写在 `pyproject.toml` 的 `[tool.importlinter]`，`make lint` 会检查。

## 开发

```bash
make install     # uv sync，需要联网
make lint        # ruff + pyright + import-linter
make test        # 默认离线，不含 real_llm 与 slow
```

解释器版本由 `.python-version` 指定（3.12），`uv` 会按需获取。
