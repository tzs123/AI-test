# 智能测试引擎

在 `backend` 目录运行：

```sh
go mod tidy
go test ./internal/agent/...
```

本地冒烟服务：`go run ./cmd/agent-server`（默认 `:8088`）。生产 Docker 默认通过 `RUNNERGO_AGENT_AUTH_VERIFY_URL` 回调 TestHub `/api/users/me/` 校验 JWT，并通过 `/api/projects/{id}/` 校验项目成员权限；未配置回调时仅用于本地开发。

合并到现有 Docker 栈：在项目根目录执行 `docker compose -f docker-compose.yaml up -d --build agent-engine testhub-frontend`。Agent 健康检查为 `http://localhost:58891/health`，统一入口为 `http://localhost:9999/api/v1/agent/*`，页面为 `http://localhost:9999/testhub/agent/`。

服务启动时注入已有认证中间件，然后注册路由：

```go
engine := agent.NewAgentEngine(llmClient, agent.NewKnowledgeBase(), nil, nil)
handler := handler.NewAgentHandler(engine, func(c *gin.Context) bool {
    _, ok := c.Get("user_id") // 使用 RunnerGo 现有认证上下文
    return ok
})
handler.RegisterRoutes(router.Group("/api/v1"))
_ = agent.Migrate(gormDB)
```

生产启动会自动选择 Redis 可靠队列（`RUNNERGO_REDIS_ADDR`、`RUNNERGO_REDIS_PASSWORD`），任务写入 MySQL 后异步执行。提交 `POST /api/v1/agent/runs` 返回 202 和任务 ID，使用 `GET /api/v1/agent/tasks/{id}` 查询，或订阅 `/api/v1/agent/tasks/{id}/events` 获取 SSE 状态；旧的 `/agent/run` 保持同步兼容。

LLM 可实现 `Generate(context.Context, string) (string, error)`、`Complete` 或 `Chat` 任一接口；引擎自动执行超时与指数退避重试。未配置 LLM 时使用内置的正向、边界、异常三类保底用例。知识库可加载 Markdown/PDF、通过 `DefectLoader` 加载 MySQL 历史缺陷，并读取 YAML 测试规范。

执行器可在工作台保存，也可调用 `GET/PUT /api/v1/agent/executor-config` 配置。API 类型支持 `base_url`、`method`、`expected_status`、`timeout_ms`、`headers` 和 `token`；`token` 和 Authorization 请求头保存后不会通过 API 回显。命令类型必须配置 `allowed_commands` 白名单。没有启用执行器时用例标记为 `skipped`，不会伪造通过结果。配置写入 MySQL 的 `agent_executor_configs` 表并在服务重启时加载。

环境变量建议：`RUNNERGO_LLM_PROVIDER=openai|local`、`RUNNERGO_LLM_API_KEY`、`RUNNERGO_LLM_MODEL`、`RUNNERGO_LLM_BASE_URL`、`RUNNERGO_AGENT_TIMEOUT`、`RUNNERGO_AGENT_RETRIES`。队列相关：`RUNNERGO_REDIS_ADDR`、`RUNNERGO_REDIS_PASSWORD`、`RUNNERGO_AGENT_QUEUE_PREFIX`。认证相关：`RUNNERGO_AGENT_AUTH_REQUIRED=true`、`RUNNERGO_AGENT_AUTH_VERIFY_URL=http://testhub-backend:8000/api/users/me/`。执行器环境变量为 `RUNNERGO_EXECUTOR_TYPE=api|command|none`、`RUNNERGO_EXECUTOR_ENABLED`、`RUNNERGO_EXECUTOR_BASE_URL`、`RUNNERGO_EXECUTOR_METHOD`、`RUNNERGO_EXECUTOR_EXPECTED_STATUS`、`RUNNERGO_EXECUTOR_TIMEOUT_MS`、`RUNNERGO_EXECUTOR_TOKEN`、`RUNNERGO_EXECUTOR_HEADERS`、`RUNNERGO_EXECUTOR_ALLOWED_COMMANDS`。
