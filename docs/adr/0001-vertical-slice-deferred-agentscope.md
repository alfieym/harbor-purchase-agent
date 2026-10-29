# ADR-0001：功能纵切，并把 AgentScope 推迟到 Phase 2

- 状态：已采纳
- 日期：2026-10-29

## 背景

README 最初按 DDD/六边形分层描述仓库：

```
harbor/domain/catalog/   纯领域
harbor/application/      用例、工具、护栏、ReAct 循环
harbor/infrastructure/   本地 JSONL catalog 仓储
harbor/presentation/     FastAPI + SSE
```

但仓库实际只有 README，没有任何代码。Phase 1 的目标是可运行、可测试、
前后端连通的骨架，当前唯一真实功能是健康检查。按四层拆分意味着为
一个 `GET /health` 建立 domain 端口、application 用例和 infrastructure
实现，抽象成本明显高于收益；后续聊天、检索、Agent 的边界也尚未被真实需求
验证。

同时，AgentScope 的接入形态（进程内 Agent 还是 Agent Service）、会话存储、
工具定义都还没有定论。此时写 `AgentRuntime` 或 Agent 工厂属于过早抽象。

## 决策

1. **后端采用功能纵切**：`features/<name>/` 自包含 router、schemas，
   按需增加 service/repo/models；`core/` 只放共享基础设施；`agents/` 是
   Phase 2 的横切层。
2. **Phase 1 完全不引入 AgentScope**：不安装依赖、不写 Agent/SSE 实现，
   只保留配置字段、目录占位 README 和 SSE 契约文档。
3. **契约先行**：REST 由 FastAPI OpenAPI 自动生成 TypeScript 类型；
   SSE 事件在 `docs/api-contract.md` 中先定义联合类型，Phase 2 实现。

## 后果

- 正面：骨架小、可测试；新增功能只需加一个 feature 目录并挂载 router；
  不会为不存在的需求维护空抽象层。
- 负面：README 原先的分层描述失效，需要同步改写；`agents/` 与
  `features/chat/` 在 Phase 2 前只是占位。
- 迁移：如果后续领域模型变复杂，可以在单个 feature 内再拆
  `models/service/repo`，不需要全仓库改分层。

## 备选方案

- **保留 DDD/六边形**：为当前唯一的 health feature 建立端口与适配器，
  成本高于收益，否决。
- **Phase 1 就接入 AgentScope**：需要先决定 Agent Service 形态、会话存储和
  模型调用方式，超出骨架范围，推迟到 Phase 2。
- **先做规则版 planner**（README 原计划）：AgentScope 2.x 的 `Agent` 本身就是
  LLM 驱动的 ReAct 循环，再维护一套规则 planner 会形成双轨，否决。
