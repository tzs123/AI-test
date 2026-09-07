# 核心功能模块 (Core App)

`apps.core` 保留 TestHub 跨模块能力，目前承载测试数据与 APP 自动化定时任务调度。

## 当前功能

### APP 自动化定时任务调度器

**命令**: `python manage.py run_all_scheduled_tasks`

**功能**:
- 检查 APP 自动化定时任务
- 按任务配置创建执行记录
- 通过 Celery 异步启动 APP 用例或套件执行

## 使用方法

### 持续运行

```bash
python manage.py run_all_scheduled_tasks
```

### 自定义检查间隔

```bash
python manage.py run_all_scheduled_tasks --interval 30
```

### 单次检查

```bash
python manage.py run_all_scheduled_tasks --once
```

## 调度器输出示例

```text
============================================================
启动 APP 自动化定时任务调度器
检查间隔: 60秒
============================================================

[2026-01-10 23:30:00] 开始检查 APP 自动化任务...
  [APP] 活跃任务数: 1
  [APP] 执行任务: 每日 APP 回归
    ✓ 任务 每日 APP 回归 已启动
✓ 本次调度执行了 1 个 APP 自动化任务
```

## 注意事项

1. 同一环境只运行一个调度器实例，避免重复触发任务。
2. 调度器依赖数据库和 Celery Worker，请先确认相关服务可用。
3. 旧 TestHub API 测试和 Web UI 自动化调度已移除。
4. UI/APP 自动化通知统一使用 RunnerGo「设置 → 第三方集成」中的已启用群机器人。
