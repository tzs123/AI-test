# 智能测试引擎 — 补齐覆盖率缺口

## Context（背景）

用户要求实现"智能测试引擎"四个模块。经探索确认：**该模块已完整实现并全部测试通过**，并非用户假设的"空目录"。用户已选择"Close the gaps（补齐缺口）"方案。实际唯一缺口是 `internal/agent` 包测试覆盖率为 **79.4%**，低于用户要求的 ≥80%。其余交付物（数据库迁移脚本、配置启动文档、Swagger 注释、前端状态页）均已存在且完整，本次仅做核验、不重写。

## 现状核验（已满足，不改动）

| 用户要求 | 现状 | 证据 |
|---|---|---|
| 模块1 AgentEngine | ✅ 已实现 | [engine.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/engine.go)、[types.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/types.go) |
| 模块2 多 Agent+Orchestrator | ✅ 已实现 | [agents.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/agents.go) |
| 模块3 KnowledgeBase | ✅ 已实现 | [knowledge.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/knowledge.go) |
| 模块4 EvaluateQuality | ✅ 已实现 | [quality.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/quality.go) |
| 数据库迁移脚本 | ✅ 已存在 | [migrations/001_agent_engine.sql](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/migrations/001_agent_engine.sql)（7 张表，与 models.go 一致） |
| 配置与启动文档 | ✅ 已存在 | [README-agent.md](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/README-agent.md)（环境变量、Docker、认证、路由、LLM、执行器、队列全覆盖） |
| Swagger 注释 | ✅ 已存在 | [handler/agent.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/handler/agent.go) 各端点均有 `@Summary/@Router` |
| 前端状态页 | ✅ 已存在 | [AgentTask.vue](file:///Users/tanzsongsen/RunnerGo/runnergo/frontend/src/views/AgentTask.vue) + [api/agent.ts](file:///Users/tanzsongsen/RunnerGo/runnergo/frontend/src/api/agent.ts) |
| 约束：不破坏响应格式 | ✅ 增量 `gin.H`，未改既有 handler |
| 约束：认证/权限集成 | ✅ `Authorize`/`AuthorizeProject`/`RemoteAuthorizer` |
| 约束：LLM 超时+重试 | ✅ `RetryLLM`+`contextWithTimeout` |
| 约束：GORM AutoMigrate | ✅ `Migrate`/`MigrateExecutorConfig` |

## 本次唯一工作：补测试使覆盖率 ≥80%（目标 ≥85%）

新增一个测试文件 [backend/internal/agent/coverage_test.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/coverage_test.go)，复用现有测试约定（`mockLLM`/`mapExecutor`/`fakeRedis`/sqlite/miniredis，见 [agent_test.go](file:///Users/tanzsongsen/RunnerGo/runnergo/backend/internal/agent/agent_test.go)），针对覆盖率最低的未覆盖分支补充用例：

1. **`MySQLDefectLoader.LoadDefects`（12.5%）** — 用 `database/sql` + sqlite3 驱动建 `defects` 表，验证成功查询路径（rows.Next/Scan/append/rows.Err）、自定义 Query 路径、扫描错误路径。这是最大单项缺口。
2. **`DurableRedisTaskQueue.Dequeue`（50%）** — nil client 路径、ctx 取消路径、JSON 解码错误（写入非法 payload 后 BRPopLPush，断言 LRem 清理并返回 decode err）。
3. **`RedisTaskQueue.Dequeue`（72.7%）** — `fakeRedis` 返回单元素（`len(v)<2`）路径、解码错误路径；`key()` 默认 key 路径。
4. **`RegisterAgent`（50%）** — 注册一个真实 mock agent，断言被追加到 `o.Agents`。
5. **`APIExecutor.Execute`（60.3%）** — 成功 GET 且 `expected_result` 子串匹配 → `Passed=true`；metadata 覆盖：`method`/`url`/`path`/`body`(string 与 object 两种)/`expected_status`(int/float64/string 三种)。
6. **`callLLM`（63.6%）** — 无 context 的 `Generate(string)` 与 `Complete(string)` 变体；context 取消使 `callWithoutContext` 提前返回 `ctx.Err()`。
7. **`OpenAIClient.Generate`（67.7%）** — 空 APIKey → `ErrLLMUnavailable`；HTTP 非 2xx；返回体无 `choices`；decode 失败（用 httptest 构造各分支）。
8. **`LocalModelClient.Generate`（68%）** — 非 2xx；decode 失败（构造非法 JSON body）。
9. **`llmQualityScore`（78.6%）** — Sscanf 失败但 regex 命中数字的回退分支（如返回 "评分为 good 90 分"）；以及 regex 无数字返回 false 的分支。
10. **`persistence.go`** — `metadataProjectID` 的 uint 分支、`createdBy` 的 int 分支、`SaveKnowledge` 空 title/content 报错、`ListResults` limit 截断（>200→50）、`SaveResult` 带 runErr 的 error 文本写入。
11. **`queue.go`** — `NewMemoryTaskQueue(size<1)` 默认为 1。
12. **`RunWorker`（60%）** — 用 miniredis + `DurableRedisTaskQueue` 触发 durable 分支的 `Ack`（成功路径）。

所有新增用例均为纯增量、不改动任何生产代码，遵循"不修改已有 API 响应格式"约束。

## 验证方式

```sh
cd backend
go build ./...                                          # 编译通过
go test ./internal/agent/... -cover                     # 通过且覆盖率 ≥80%（目标 ≥85%）
go test ./internal/handler/... ./internal/service/... ./internal/model/...  # 全绿
go vet ./internal/agent/...                             # 无告警
```

可选端到端冒烟（用户原验证方式之一）：
```sh
RUNNERGO_AGENT_AUTH_REQUIRED=false go run ./cmd/agent-server &
curl -s -X POST localhost:8088/api/v1/agent/test-cases -H 'Content-Type: application/json' -d '{"name":"登录","description":"用户登录功能"}'
# 返回含 test_cases 的 JSON
```

## 不做的事

- 不重写已实现且通过测试的四个模块（避免回归风险）。
- 不重新生成迁移脚本/文档（已存在且完整，仅在上方表格核验）。
- 不改动既有 API 响应格式、handler、service、前端。
- 不引入新依赖（miniredis/sqlite3/redis 已在 go.mod）。
