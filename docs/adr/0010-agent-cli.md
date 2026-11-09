[English](0010-agent-cli.en.md)

# 0010：Shell 智能体通过 labctl 操作设备

## 状态

已接受：2026-11-09。取代 [0008](0008-agent-approvals.md)。

## 背景

实验室是测试台。所有者需要智能体运行和测试每台设备。ADR 0008 要求 LLM 智能体的设备动作由人员明确批准。在 Olares 的 Shell 中运行的智能体需要直接控制设备。

## 决定

- Olares 上的 Shell 智能体可以用 `labctl` 设备命令组操作设备。见[智能体命令行](../agent-cli.md)。
- Lab Console 持有智能体租约。智能体会话是控制台页面 `agent-<session>`，空闲超时为 10 分钟。
- 各服务保留 AGENTS.md 中的测试台保护。命令行不增加检查、配置或令牌。
- 没有 MCP 网关，也没有提案流程。

## 影响

Overview **Control** 卡片显示每个智能体持有者。所有者用 **Release all** 收回控制权。停止从不需要控制权。
