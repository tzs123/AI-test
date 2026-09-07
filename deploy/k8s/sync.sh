#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE="origin"
BRANCH="main"

export PATH="$HOME/go/bin:$HOME/brew/bin:$PATH"

usage() {
  echo "Usage: $0 {push|pull|deploy|status|restart}"
  echo "  push    - 提交本地变更并推送到 GitHub"
  echo "  pull    - 从 GitHub 拉取最新代码"
  echo "  deploy  - 部署/更新 RunnerGo 到本地 k8s"
  echo "  status  - 查看 k8s 部署状态"
  echo "  restart - 重启所有 RunnerGo pod"
  exit 1
}

cmd_push() {
  cd "$REPO_DIR"
  git add -A
  git commit -m "sync: $(date '+%Y-%m-%d %H:%M:%S')" || echo "nothing to commit"
  git push "$REMOTE" "$BRANCH"
  echo "pushed to GitHub"
}

cmd_pull() {
  cd "$REPO_DIR"
  git pull "$REMOTE" "$BRANCH"
  echo "pulled from GitHub"
}

cmd_deploy() {
  cd "$REPO_DIR"
  helm upgrade --install runnergo deploy/k8s/runnergo
  echo "deployed to k8s"
}

cmd_status() {
  kubectl get pods -n runnergo-app -o wide
  echo "=== Services ==="
  kubectl get svc -n runnergo-app
}

cmd_restart() {
  kubectl delete pods --all -n runnergo-app
  echo "restarted all pods"
}

case "${1:-}" in
  push)   cmd_push ;;
  pull)   cmd_pull ;;
  deploy) cmd_deploy ;;
  status) cmd_status ;;
  restart) cmd_restart ;;
  *)      usage ;;
esac