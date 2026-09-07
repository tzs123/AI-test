# UI 自动化全链路压测模块 — 核对与缺口修复

## Context（为什么做这件事）

用户要求"实现核心 LoadRunner 能力：业务链路多场景、升稳降压、Pacing、集合点、分布式节点、资源监控、事务分析、SLA、JSON/CSV/HTML/Markdown 导出；不是 JMeter 接口模式。JVM/数据库深度监控和完整商业协议库仍需接入"。

经核对，这套能力**已在 `auto-test/backend/load_test.py`（2654 行）完整实现**，VU 真实启动 Playwright Chromium 驱动 `run_web_flow` / `WebFlowRunner.run()`（UI 流程），不是 JMeter HTTP 模式。因此不做重写（避免回归），仅修复发现的唯一真实缺口并验证。

## 能力核对（已实现，附代码位置）

| 用户要求 | 实现位置 | 状态 |
|---|---|---|
| 业务链路多场景 | [load_test.py:1521](file:///Users/tanzsongsen/RunnerGo/runnergo/auto-test/backend/load_test.py) `_flow_for_iteration`、L741 `_chain_completeness`、L2271 `scenarios()`、`scenario_weights` | ✅ |
| 升稳降压 | L1020 `LoadRunState.target_vus(elapsed)`（ramp-up/steady/ramp-down 曲线） | ✅ |
| Pacing | L1575 `_post_iteration_wait`（VU 循环 L1699 接入）、`pacing_seconds`/`pacing_random_pct` 配置 | ✅ |
| 集合点 | L1555 `_rendezvous_wait`（VU 循环 L1667 接入）、`rendezvous_enabled` | ✅ |
| 分布式节点 | L109 `_allocate_vu_shards`、L138 `_prepare_distributed_run`、L1837 `_run_distributed_controller` | ✅ |
| 资源监控 | L361 `_ResourceSampler`（CPU/内存/网络/进程）、L488 `_resource_summary`、L455 `_collect_external_monitoring`（Prometheus） | ✅ |
| 事务分析 | L695 `_normalize_transaction_defs`、L811 `_transaction_map`、L840 `_transaction_name_for_result`、L1305 `summary` 事务聚合 | ✅ |
| SLA | L1271 `_sla_result`（TPS/P95/P99/错误率/链路成功率） | ✅ |
| JSON/CSV/HTML/Markdown 导出 | L2609 `export_run`（含 `_export_csv`、HTML、Markdown，设 `Content-Disposition`） | ✅ |
| 不是 JMeter 接口模式 | L1618 `_run_virtual_user`：`needs_browser = mode in {"ui","mixed"}` → 启动 Chromium → `run_web_flow(browser, flow)` | ✅ |
| JVM/数据库深度监控 | — | 用户明确"仍需接入"，不在本次范围 |
| 完整商业协议库 | — | 用户明确"仍需接入"，不在本次范围 |

## 唯一真实缺口（待修复）

**Bug**：`_normalize_config` 在 [load_test.py:2008](file:///Users/tanzsongsen/RunnerGo/runnergo/auto-test/backend/load_test.py) 直接访问 `body.monitoring`，但同函数 L1958/L1961 对 `execution_mode`/`agent_count` 用的是 `getattr(body, ..., default)` 防御式写法——不一致。单元测试 `test_yaml_load_profile_produces_business_transaction_metrics` 用最小 `Body` stub（无 `monitoring` 字段）调 `_normalize_config`，触发 `AttributeError: 'Body' object has no attribute 'monitoring'`。

生产 `LoadRunIn`（L943）有 `monitoring` 字段，所以生产路径不崩；但 stub/老客户端会崩，且与函数内既有 getattr 模式不一致。

**修复**（1 行，与既有模式一致）：

```python
# load_test.py:2008
monitoring = _normalize_monitoring_config(getattr(body, "monitoring", None) or profile.get("monitoring") or {})
```

理由：与 L1958 `getattr(body, "execution_mode", "auto")`、L1961 `getattr(body, "agent_count", 0)` 同模式；生产 LoadRunIn 行为不变；让 `_normalize_config` 对部分 body 对象（单测 stub、旧 API 客户端）健壮。

## 验证

- 在容器 `runnergo-auto-test-1` 跑 `tests/unit/test_load_test.py` + `test_loadrunner_manual.py` + `test_ui_shell.py` 中 load 相关用例
- 修复前：45 个，44 通过，1 失败（L2008）
- 修复后预期：45 全通过
- 不做重写，无回归风险

## 不做改动

- 不重写 load_test.py 任何已实现能力
- 不接入 JVM/数据库深度监控与商业协议库（用户明确标注"仍需接入"，超出本次范围）
