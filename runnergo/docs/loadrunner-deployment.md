# LoadRunner 11 合规部署说明

## 环境结论

`setup.exe` 是 32 位 Windows PE 程序。当前主机是 Apple Silicon macOS，Docker 服务端是 Linux/arm64，因此 LoadRunner 11 的 Controller、VuGen、Analysis 和 Load Generator 不能在当前 Docker 栈中原生启动。

LoadRunner 11 应安装在受支持的 Windows x64 虚拟机或独立 Windows 主机中。RunnerGo 可以继续在 Docker 中运行，二者通过测试网络协作。

## 授权要求

只使用 Micro Focus/OpenText 官方安装介质、试用许可证或企业许可证。不要执行或复制第三方破解程序、替换 DLL 或修改许可证校验；本项目不会读取 `/Users/tanzsongsen/Downloads/lr11安装文件/破解` 目录。

## 启动 RunnerGo

```text
cd /Users/tanzsongsen/RunnerGo/runnergo
docker compose up -d
```

前端入口：`http://localhost:9999`。Compose 中的服务已配置自动重启；LoadRunner 本体的自动启动应在 Windows 虚拟机内通过 Windows 服务、任务计划程序或虚拟机自启动完成。

## Windows 端安装流程

1. 创建隔离的 Windows 10/11 x64 虚拟机，配置固定 IP 或可解析主机名。
2. 挂载合法安装介质，运行 `setup.exe`，按授权选择 Controller、VuGen、Analysis 和 Load Generator。
3. 使用官方 License Utility 导入许可证，重启相关服务并确认 License 状态。
4. 配置被测系统地址、Load Generator 和防火墙规则，只开放必要端口。
5. 用 1 个虚拟用户回放脚本，确认登录、参数关联、事务和检查点，再逐级增加并发。

## RunnerGo 压测流程

1. 项目管理中创建项目，填写测试环境 Base URL。
2. 用例管理或 UI 场景编排中准备场景、变量、数据和断言。
3. 全链路压测中设置执行模式、VU、升压/稳态/降压时间和 SLA。
4. 启动前确认目标系统已授权、监控和回滚措施已就绪。
5. 在测试报告中核对 TPS、P95/P99、错误率、SLA 和数据完整性。

## 常见排障

- Docker 页面不可用：检查 `docker compose ps`、端口 `9999`、登录态以及 Redis/MySQL 健康状态。
- Windows 客户端无法连接：检查虚拟机网络、防火墙、DNS、许可证服务器和 Load Generator 心跳。
- 指标异常：先缩小到 1 VU，确认目标 URL、认证、数据关联和时间同步。
- 向官方支持提交问题时保留版本号、安装日志、License 状态和最小复现脚本，移除敏感业务数据。
