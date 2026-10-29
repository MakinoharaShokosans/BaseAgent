# 测试套件现状与评测

本文记录**实际存在**的测试组织方式、黄金场景是怎么落地的、前端测试的范围，以及评测框架 `baseagent-bench`。分层与原则见 [01_strategy](01_strategy.md)，按子系统的清单见 [02_test_matrix](02_test_matrix.md)。

## 1. 后端测试的组织

`backend/tests/` 下按被测模块平铺，一个模块一个 `test_*.py`（约 80 个文件，约 2000 个用例），共用的夹具和假对象放在不带 `test_` 前缀的模块里：

| 文件 | 用途 |
|---|---|
| `conftest.py` | 全局夹具：`make_state`、隔离的数据目录 |
| `loop_support.py` | `ScriptedWorld`：可编排的假模型与假工具（等待、报错、泄露金丝雀、需要审批） |
| `gateway_support.py` | 假网关与结构化输出、带工具调用的响应构造器 |
| `api_support.py` | 内存 ASGI 的 API 装置 |
| `mcp_fixture_server.py`、`sdk/` | 进程内的 MCP 替身；对官方 SDK 的兼容探针 |

标记在 `pyproject.toml` 的 `[tool.pytest.ini_options]`：`unit`、`integration`、`e2e`、`security`、`slow`、`real_llm`；默认 `-m "not real_llm and not slow"`，离线运行。

约定：数据目录一律指向 `tmp_path`；外部命令和网络用内存 ASGI 或进程内替身；属性测试用 Hypothesis（状态编解码、折叠单调、切片等）。

## 2. “黄金场景”是怎么落地的

没有固化的 JSON 轨迹文件。同样的场景由脚本化的假模型驱动、用断言校验，分散在这些文件里：

| 场景 | 在哪 |
|---|---|
| 直接答复、单工具往返、没有步数上限 | `test_loop_kernel.py` |
| 审批挂起与续跑、批准、拒绝 | `test_loop_kernel.py`、`test_resume.py`、`test_approval_binding.py` |
| 连续出错、死循环拦截 | `test_loop_guards.py`、`test_loop_tools.py` |
| 并发只读、串行写入 | `test_loop_tools.py` |
| 金丝雀泄露终止 | `test_canary.py`、`test_loop_kernel.py` |
| 网关重试与降级 | `test_llm_resilience.py`、`test_llm_gateway.py` |
| 子智能体委派与权限收窄 | `test_delegate.py` |
| 提交到交付的完整链路（真实工具、真实 HTTP） | `test_application_e2e.py` |
| 任务内压缩与溢出恢复 | `test_task_compaction.py`、`test_loop_kernel.py` |
| 检查点恢复等价 | `test_checkpoint_store.py`、`test_recovery.py` |

不变量靠断言保证：原子配对（`check_invariants`）、先提交后发事件、检查点往返相等。

**没有做的**：固化的黄金轨迹文件与逐项对照；`real_llm` 标记的真实模型测试（标记已注册，目前没有用例）；严格 `xfail` 记录“已知防不住”的安全边界（目前只有一个条件跳过）；CI 配置（只有 `pre-commit` 与 `make check`）。

## 3. 前端测试

Vitest 加 Testing Library，测试文件放在源文件旁边（`*.test.ts(x)`），共 13 个文件、80 个左右用例：API 客户端与错误信封、SSE 重连与游标、事件归约（`taskReducer`，用事件序列回放）、审批卡片、输入台、侧边栏与菜单、Markdown 渲染转义等。`frontend/e2e/` 目前只有一份 README，没有 Playwright 用例。

## 4. 评测框架（`baseagent-bench`）

评测是黑盒统计型评估，通过公共 HTTP API 驱动运行中的真实服务（需要先 `baseagent start`），默认不运行，会花 Token：

```bash
baseagent-bench --list                                   # 内置用例（11 个）
baseagent-bench --repeat 3 --label baseline              # 跑并生成 eval_reports/*.md|json
baseagent-bench --compare eval_reports/a.json eval_reports/b.json
```

内置用例覆盖读代码问答、新建脚本、修测试、重命名重构、改配置、注入与只读拒绝写、实现函数、两处 bug、跨模块追踪、缺信息时不编造。

指标全部来自服务端事件，不靠模型自评：

1. **成功率**：基于文件系统终态、命令退出码与输出的机械检查；
2. **回合数与 Token**：中位数与 P90；
3. **缓存命中率、各调用用途的 Token 占比**（来自 `llm.call`）；
4. **工具失败率、循环拦截与连续出错提醒次数**；
5. **网关重试与错误**。

成本保护：`--max-total-tokens` 总配额，达到后停止后续用例；单个用例有超时。

当前没有基于现行架构的真实模型基线。改了提示词或上下文策略后，前后各跑一次再 `--compare`。
