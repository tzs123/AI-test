# RunnerGo 本地 K8s 部署

## 架构

- **K8s 集群**: k3d (k3s in Docker)，单节点，本地开发
- **部署方式**: Helm Chart
- **端口映射**: k3d loadbalancer → NodePort → 主机端口

## 前置条件

```bash
# 已安装（通过 go install / curl）
kubectl, helm, k3d, docker
```

## 快速开始

### 1. 创建 k8s 集群

```bash
k3d cluster create runnergo \
  --image rancher/k3s:v1.30.4-k3s1 \
  --port "9999:30999@loadbalancer" \
  --port "8000:30080@loadbalancer" \
  --port "3307:30307@loadbalancer" \
  --port "6380:30380@loadbalancer" \
  --port "27018:30018@loadbalancer" \
  --port "9090:30090@loadbalancer" \
  --port "3000:30000@loadbalancer" \
  --port "58908:30908@loadbalancer" \
  --volume "$(pwd)/runnergo:/runnergo@all" \
  --k3s-arg "--disable=traefik@all"
```

### 2. 导入本地镜像

```bash
k3d image import \
  runnergo-agent-engine:local \
  runnergo-testhub-frontend:local \
  runnergo-testhub-backend:local \
  runnergo-testhub-skill-runner:local \
  runnergo-auto-test:local \
  -c runnergo
```

### 3. 部署

```bash
helm install runnergo deploy/k8s/runnergo
```

### 4. 查看状态

```bash
kubectl get pods -n runnergo-app
```

## 同步流程

```bash
# 本地代码 → GitHub
./deploy/k8s/sync.sh push

# GitHub → 本地
./deploy/k8s/sync.sh pull

# 更新 k8s 部署
./deploy/k8s/sync.sh deploy

# 查看状态
./deploy/k8s/sync.sh status
```

## 访问地址

| 服务 | 地址 |
|------|------|
| TestHub 前端 | http://localhost:9999 |
| TestHub API | http://localhost:8000 |
| MySQL | localhost:3307 |
| Redis | localhost:6380 |
| MongoDB | localhost:27018 |
| Ollama | localhost:58908 |
| Prometheus | localhost:9090 |
| Grafana | localhost:3000 |

## 架构问题（arm64 Mac）

RunnerGo 的预构建镜像为 linux/amd64。k3d 节点为 arm64，无法直接运行 amd64 镜像。

**解决方案（任选其一）**:

1. **Docker Desktop 内置 K8s**（推荐）: Settings > Kubernetes > Enable，原生支持 Rosetta 2 运行 amd64 镜像
2. **重建 arm64 镜像**: `docker build --platform linux/arm64 -t runnergo-xxx:local .`
3. **k3d + binfmt**: 注册 qemu binfmt 多架构支持