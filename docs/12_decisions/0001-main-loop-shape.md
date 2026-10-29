# 0001 主循环形态探索（五阶段架构）

> [!CAUTION]
> **已废弃（SUPERSEDED）**：本决策已被 [ADR 0006：纯粹智能体循环架构](0006-pure-agent-loop.md) 取代。BaseAgent 现行架构采用单主循环模型自主决策机制，五阶段编排模型已全面废弃。本文档仅作为架构决策演进记录归档。

- 状态：**已废弃（SUPERSEDED by [0006](0006-pure-agent-loop.md)）**
- 现行方案：[0006-pure-agent-loop.md](0006-pure-agent-loop.md)
- 影响模块：[02_runtime/02_main_loop.md](../02_runtime/02_main_loop.md)

## 背景

在主循环架构选型初期，关于智能体循环形态的讨论。

## 选项

**A. 保持 planner / budget_guard / executor / tool_runner / evaluator 五阶段，用显式循环加路由表驱动。**

- 沿用多阶段流水线处理，划分规划与复核。
- 代价：每一步经历多次模型调用，Token 成本和响应延迟偏高，状态流转僵化。

**B. 收敛为单一 Agent 循环（模型自主规划与工具调用）。**

- 代码极简，调用链短，延迟与 Token 消耗大幅降低。
- 依赖模型自身的长上下文与推理规划能力。

## 历史决议与演进

早期曾选择方案 A 作为过渡探索，但在实际落地中五阶段导致严重的 Token 浪费、延迟放大和状态割裂。因此，该决策已被 [0006 纯粹智能体循环架构](0006-pure-agent-loop.md) 完全取代，系统全面转向模型自主规划与纯粹循环机制。
