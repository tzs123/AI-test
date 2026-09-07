# Windows 虚拟机与 RunnerGo 连接

当前 Mac 主机局域网地址：`192.168.0.92`。Docker 已发布 RunnerGo 前端 `9999` 端口和后端 `8000` 端口。

## 1. 虚拟机选择

- Apple Silicon Mac：使用 Parallels、VMware Fusion 或 UTM 安装合法 Windows 11 ARM64。
- 需要稳定运行 LoadRunner 11：优先使用 Intel/AMD Windows x64 电脑或远程 Windows x64 虚拟机。Windows ARM 的 x86 模拟不保证 LoadRunner 11、驱动和许可证组件兼容。
- 当前主机没有检测到 Parallels、UTM、VMware 或 Windows 镜像，因此本次没有创建虚拟机。

## 2. 网络模式

推荐先使用虚拟机软件的共享网络/NAT 模式。Windows 虚拟机内不要访问 `localhost:9999`，应访问 Mac 宿主机地址：

```text
http://192.168.0.92:9999
```

如使用桥接网络，改用 Mac 当前局域网地址；地址变化后同步更新脚本参数。Windows 与 Mac 必须处于同一网络，且 Mac 防火墙不能拦截 Docker Desktop 的已发布端口。

## 3. Windows 侧验证

将 `scripts/check-runnergo-from-windows.ps1` 复制到 Windows 虚拟机，在 PowerShell 中执行：

```powershell
.\check-runnergo-from-windows.ps1 -RunnerGoHost 192.168.0.92
```

脚本会检查 TCP `9999`、TCP `8000` 和前端 `/health`。通过后，在 Windows 浏览器打开 `http://192.168.0.92:9999` 登录 RunnerGo。

## 4. LoadRunner 配合边界

RunnerGo 与 LoadRunner 目前是并行工具：RunnerGo 负责项目、场景、参数、压测编排和报告；LoadRunner 11 的 Controller/VuGen/Analysis 仍在 Windows 环境内运行。不要把 Windows 安装目录、许可证文件或修改过的 DLL 挂载进 Linux Docker 容器。

如需让 Windows 访问 RunnerGo API，使用 `http://192.168.0.92:8000`；如需访问前端，使用 `http://192.168.0.92:9999`。不要把这些端口暴露到公网。

## 5. 启动顺序

1. 启动 Docker Desktop。
2. 在项目目录运行 `docker compose up -d`。
3. 启动 Windows 虚拟机并运行连通性脚本。
4. 在 Windows 中安装并通过官方许可证验证 LoadRunner。
5. 先用 1 VU 验证脚本，再逐步扩大负载。
