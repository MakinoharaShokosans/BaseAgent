# 0010 工具调用场景下的深度思考链解锁与防御性全量回传

- 状态：**已采纳**
- 影响：[07_llm_gateway/03_reasoning_content.md](../07_llm_gateway/03_reasoning_content.md)、[07_llm_gateway/04_deepseek.md](../07_llm_gateway/04_deepseek.md)、`config/config.toml`、`backend/src/agent_runtime/llm/wire.py`、`backend/src/agent_runtime/llm/gateway.py`

## 背景

在 Day-0 官方基线评测（`baseline_v1`，11/11 PASS）中，评测数据显示：
- **Reasoning Tokens 消耗为 0**；
- 模型 Prompt Tokens 占总开销的 98.6%，Completion Tokens 仅占 1.4%。

其根因在于：此前为了遵守 0003 决策并规避 DeepSeek 在带 `tools` 时若未回传完整历史思考导致的 HTTP 400 校验错误，配置层默认设置了 `thinking_with_tools = false` 且 `reasoning_passback = "never"`。当主循环带工具执行时，网关强制发送 `thinking: {"type": "disabled"}`，导致模型完全丧失了在复杂代码修复、跨模块追踪等任务中本可发挥的深思熟虑能力（CoT）。

## 选项

**A. 维持现状（带工具不开思考）**
- 优点：绝对不会触发 DeepSeek 400 思考链校验异常。
- 缺点：模型在代码重构、多文件排错等高难度任务中丧失深度思考能力，全靠单步直觉反应，天花板受限。

**B. 激进开启（直接开启思考并配置全量回传，无容错）**
- 将 `thinking_with_tools` 置为 true，回传策略设为 `all_when_tools`。
- 缺点：一旦遇到历史助手消息缺少 `reasoning`（例如会话历史注入、跨模型端点降级、或者老任务恢复），DeepSeek 会因缺少前置思考内容直接返回 HTTP 400，导致任务崩盘。

**C. 契约解锁 + 防御性降级 + 真实用量透传（推荐）**
1. **契约解锁**：在 `models.reasoning` 端点开启 `thinking_with_tools = true` 与 `reasoning_passback = "all_when_tools"`。
2. **防御性降级（Defensive Degradation）**：网关在构造线上请求时动态校验——若发现请求带工具，且存在历史助手消息未携带有效思考内容或来自不同端点，网关在当前单次请求中**自动将思考模式退化为 `thinking: disabled`**，坚决不向服务商发送非法的思考请求，100% 避免 400 崩溃。
3. **真实用量透传（Stream Usage Capture）**：流式调用 `_stream_once` 启用 `stream_options: {"include_usage": true}`，精准捕获 DeepSeek 回传的 `reasoning_tokens`、缓存命中统计（`prompt_cache_hit_tokens`），并在降级估算分支中补齐思考 Token 统计，使评测与审计可准确量化思考开销与缓存收益。

## 决定

采纳选项 C。

## 后果

1. `config/config.toml` 中 `models.reasoning` 具备带工具深度推理能力。
2. `wire.py` 严格遵守 DeepSeek 服务商协议，在具备完整思考链时全量回传，在思考链不完备时无感退化，具备最高韧性。
3. `gateway.py` 的流式与非流式调用均可透明获得 `reasoning_tokens` 与缓存命中指标。
4. 评测框架 `baseagent-bench` 可直接产出基线对比报告，客观衡量思考链开启后的 Token 成本、时延与表现变化。
