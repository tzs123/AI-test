#!/usr/bin/env bash
set -euo pipefail

if [[ "${TESTHUB_START_APPIUM:-1}" != "1" ]] || ! command -v appium >/dev/null 2>&1; then
  return 0 2>/dev/null || exit 0
fi

instance_suffix="${1:-}"
log_suffix=""
if [[ -n "${instance_suffix}" ]]; then
  log_suffix="-${instance_suffix}"
fi

mkdir -p \
  /app/logs/app_automation \
  "${APPIUM_CHROMEDRIVER_DIR:-/app/logs/app_automation/chromedriver}"

adb_upstream_socket="${ADB_SERVER_SOCKET:-}"
local_adb_port="${APPIUM_LOCAL_ADB_PORT:-5037}"
if [[ "${adb_upstream_socket}" == tcp:* ]]; then
  adb_proxy_target="${adb_upstream_socket#tcp:}"
  adb_proxy_host="${adb_proxy_target%:*}"
  adb_proxy_upstream_port="${adb_proxy_target##*:}"
  if [[ -n "${adb_proxy_host}" && "${adb_proxy_host}" != "127.0.0.1" && "${adb_proxy_host}" != "localhost" ]]; then
    adb_proxy_args=(
      --listen-port "${local_adb_port}"
      --upstream-host "${adb_proxy_host}"
      --upstream-port "${adb_proxy_upstream_port}"
    )
    adb_recovery_url="${ADB_RECOVERY_URL:-}"
    if [[ -z "${adb_recovery_url}" && "${adb_proxy_host}" == "host.docker.internal" ]]; then
      adb_recovery_url="http://host.docker.internal:8765/adb/ensure"
    fi
    if [[ -n "${adb_recovery_url}" ]]; then
      adb_proxy_args+=(--recovery-url "${adb_recovery_url}")
    fi
    python /app/docker/adb_proxy.py "${adb_proxy_args[@]}" \
      > "/app/logs/app_automation/adb-proxy${log_suffix}.log" 2>&1 &
    export ADB_SERVER_SOCKET="tcp:127.0.0.1:${local_adb_port}"
    echo "ADB proxy started on 127.0.0.1:${local_adb_port} -> ${adb_proxy_host}:${adb_proxy_upstream_port}"
  fi
fi

appium_args=(
  --address 127.0.0.1
  --port "${APPIUM_PORT:-4723}"
  --base-path /
)
if [[ -n "${APPIUM_ALLOW_INSECURE:-}" ]]; then
  appium_args+=(--allow-insecure "${APPIUM_ALLOW_INSECURE}")
fi
appium "${appium_args[@]}" \
  > "/app/logs/app_automation/appium${log_suffix}.log" 2>&1 &
echo "Appium server started on 127.0.0.1:${APPIUM_PORT:-4723}"

for ((attempt = 1; attempt <= 30; attempt++)); do
  if curl -fsS "http://127.0.0.1:${APPIUM_PORT:-4723}/status" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
