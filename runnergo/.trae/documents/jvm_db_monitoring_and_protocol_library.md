# JVM/数据库深度监控 + 完整商业协议库 — 实现计划

## Context

之前 UI 全链路压测核对中，这两项标注"仍需接入"。用户现要求实现。经探索：

- **监控侧已有脚手架**：`EXTERNAL_MONITOR_FIELDS`(load_test.py:56-65) 已支持 4 个 JVM + 4 个 DB 指标，但只走 Prometheus（`_collect_external_monitoring` L455）。`_resource_summary`(L488) 已自动聚合 `EXTERNAL_MONITOR_FIELDS` 的所有 key，`availability` 已预留 `jvm`/`database`。所以"深度监控"= 扩展指标集即可，无需新客户端。
- **协议侧已有脚手架**：`execute_step`(web_flow_runner.py:4078) 已按 action 分发——`api/http`→`execute_api_step`、`db/database/query/sql`→`execute_database_step`；`NON_BROWSER_ACTIONS`(L63) 已注册 db 类动作；`classify_flow`(L858) 已按动作计数。所以"协议库"= 按同模式注册新动作 + 加分发分支 + 实现驱动，复用 `validate_outbound_url`/pre-post 脚本/extract/assertions/连接复用语义。

用户决策：监控走 **Prometheus 指标集扩展**（最轻、与现有架构一致）；协议库按"完整"意图实现 **4 组协议**，做成可扩展驱动框架。

## 模块一：JVM/数据库深度监控（Prometheus 扩展）

**改 [load_test.py:56-65](file:///Users/tanzsongsen/Runnergo/runnergo/auto-test/backend/load_test.py) `EXTERNAL_MONITOR_FIELDS`**，新增深度指标并归类：

```
JVM 深度（新增 8 个）：jvm_heap_eden_mb / jvm_heap_survivor_mb / jvm_heap_old_mb /
  jvm_gc_marksweep_count / jvm_gc_marksweep_time_ms / jvm_gc_scavenge_count /
  jvm_gc_scavenge_time_ms / jvm_threads_blocked / jvm_threads_waiting / jvm_classes_loaded
DB 深度（新增 7 个）：db_active_connections / db_pool_max / db_pool_wait_ms /
  db_slow_query_samples / db_replication_lag_seconds / db_table_locks / db_innodb_row_ops_per_sec
```

**无需改其它代码**：`_normalize_monitoring_config`(L413) 按 schema 校验、`_collect_external_monitoring`(L455) 自动拉取、`_resource_summary`(L488) 用 `*EXTERNAL_MONITOR_FIELDS.keys()` 自动聚合、`availability` 已预留。新增字段自动流入 summary/导出。

**配套**：在 `/capabilities`(L2227) 返回里列出全部监控字段名 + 推荐 PromQL 样例（jmx_exporter / mysqld_exporter / postgres_exporter），方便配置。

## 模块二：商业协议库（可扩展驱动框架 + 4 组协议）

### 框架（新包 `auto-test/core/protocol_drivers/`）

- `base.py`：`ProtocolDriver` 协议（`name`、`actions: set[str]`、`execute(step, context) -> dict`、`acquire(context)`/`release(context)` 连接生命周期）；`DRIVERS` 注册表 + `driver_for(action)` 查找；结果 dict 对齐 api 形状（`status/url/body/text/extracted/scripts/business`），以便复用 `record_iteration` + 业务事务 + 导出。
- `__init__.py`：注册各驱动，导出 `PROTOCOL_ACTIONS`（供 `NON_BROWSER_ACTIONS` 合并）。

### 各驱动（每驱动一文件，stdlib 优先，第三方可选依赖 + 友好 ImportError 提示）

| 驱动文件 | 协议/动作 | 客户端 |
|---|---|---|
| `tcp_socket.py` | `tcp`/`socket`/`websocket` | stdlib `socket`；WebSocket 用 stdlib 握手或可选 `websockets` |
| `messaging.py` | `kafka`/`rabbitmq`/`amqp` | `kafka-python`、`pika`（可选） |
| `rpc.py` | `grpc`/`dubbo` | `grpcio`（可选）；Dubbo 走 HTTP 网关或 Triple，无成熟 Python 客户端则文档说明 |
| `transfer.py` | `ftp`/`sftp`/`smtp`/`imap`/`pop3`/`ldap` | stdlib `ftplib`/`smtplib`/`imaplib`/`poplib`；`paramiko`(SFTP)、`ldap3`(LDAP) 可选 |

每驱动：URL 类用 `validate_outbound_url` 防 SSRF；变量解析复用 `_step_runtime_context`；连接对象挂到 `context.protocol_sessions[driver_name]`，按 VU 复用（对齐 `http_session` 连接复用语义 L2030 `connection_reuse`）；超时/重试对齐 LLM 调用风格。

### 接入点（改 2 处现有文件）

1. **[web_flow_runner.py:63](file:///Users/tanzsongsen/RunnerGo/runnergo/auto-test/core/web_flow_runner.py) `NON_BROWSER_ACTIONS`**：合并 `PROTOCOL_ACTIONS`，使 `classify_flow` 把新动作当 protocol 步骤（推荐 `protocol` 模式）。
2. **[web_flow_runner.py:4078+](file:///Users/tanzsongsen/RunnerGo/runnergo/auto-test/core/web_flow_runner.py) `execute_step`**：在 `api`/`db` 分支后加 `elif action in PROTOCOL_ACTIONS: result = driver_for(action).execute(step, self.runtime_context)`，复用现有 `last_locator_diagnostics`/`_attach_runtime_data`/异常 `runtime_result` 传播。
3. **[load_test.py:2227](file:///Users/tanzsongsen/RunnerGo/runnergo/auto-test/backend/load_test.py) `/capabilities`**：声明 `protocols: [...]` 全集 + 监控字段全集。

### 不改动

- 不重写 `execute_api_step`/`execute_database_step`（保持兼容，新驱动走独立注册）。
- 不改压测运行时主循环（`_run_virtual_user` 已通过 `WebFlowRunner` 透明支持新动作）。

## 测试

- **新** `auto-test/tests/unit/test_protocol_drivers.py`：每驱动 mock 客户端，验证结果形状、action 注册、SSRF 拒绝、可选依赖缺失时抛清晰错误。
- **扩展** `auto-test/tests/unit/test_load_test.py`：新增 EXTERNAL_MONITOR_FIELDS 字段进入 `_normalize_monitoring_config` 校验 + `_resource_summary` 聚合 + `/capabilities` 列出。
- 容器 `runnergo-auto-test-1` 跑 `tests/unit/test_protocol_drivers.py tests/unit/test_load_test.py tests/unit/test_web_flow_runner.py`，全绿且无回归。

## 验证

1. 容器内跑上述单测全通过。
2. 配置一个含 `kafka`/`tcp`/`ftp`/`ldap` 动作的 YAML，`classify_flow` 应推荐 `protocol`；`_run_virtual_user` protocol 分支应调到对应驱动（用 mock 客户端在单测验证端到端，不依赖真实 broker/服务）。
3. 配置 `monitoring.queries` 含新 jvm_*/db_* 字段，`_collect_external_monitoring`（mock Prometheus 响应）应取值并进 `_resource_summary.metrics.avg/peak`。
4. `/capabilities` 接口返回新协议 + 新监控字段。

## 范围说明

- Dubbo 真二进制协议无成熟 Python 客户端：走 HTTP 网关/Triple 路径并在文档标注；其余协议均真实实现。
- 第三方依赖（kafka-python/pika/grpcio/paramiko/ldap3/websockets）设为可选，缺失时驱动抛 `RuntimeError("需安装 X：pip install ...")`，不影响平台启动与其它协议。
