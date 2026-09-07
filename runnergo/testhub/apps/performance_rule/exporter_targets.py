from __future__ import annotations

import http.client
import json
import os
import subprocess
import tempfile
import socket
from pathlib import Path
from typing import Any

import requests
import yaml
from django.conf import settings

from .tested_database_config import load_tested_database_config


PROCESS_FILE = 'remote-process-exporters.yml'
JMX_FILE = 'remote-jmx-exporters.yml'
MYSQL_FILE = 'remote-mysql-exporters.yml'


def get_target_dir() -> Path:
    configured = getattr(settings, 'PROMETHEUS_TARGET_DIR', '')
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR).parent / 'deploy' / 'prometheus' / 'targets'


def get_deploy_compose_file() -> Path:
    configured = getattr(settings, 'DEPLOY_COMPOSE_FILE', '')
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR).parent / 'deploy' / 'docker-compose.yml'


def get_docker_socket_path() -> Path:
    return Path(getattr(settings, 'DOCKER_SOCKET_PATH', '/var/run/docker.sock'))


def _target_path(kind: str) -> Path:
    if kind == 'process':
        filename = PROCESS_FILE
    elif kind == 'jmx':
        filename = JMX_FILE
    elif kind == 'mysql':
        filename = MYSQL_FILE
    else:
        raise ValueError('unsupported exporter type')
    return get_target_dir() / filename


def _empty_or_list(data: Any) -> list[dict[str, Any]]:
    if data in (None, ''):
        return []
    if not isinstance(data, list):
        raise ValueError('target file must contain a YAML list')
    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        targets = item.get('targets') or []
        labels = item.get('labels') or {}
        if isinstance(targets, list):
            normalized.append({
                'targets': [str(target) for target in targets],
                'labels': labels if isinstance(labels, dict) else {},
            })
    return normalized


def read_target_configs(kind: str) -> list[dict[str, Any]]:
    path = _target_path(kind)
    if not path.exists():
        return []
    return _empty_or_list(yaml.safe_load(path.read_text(encoding='utf-8')))


def _write_target_configs(kind: str, configs: list[dict[str, Any]]) -> None:
    path = _target_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = configs if configs else []
    content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if payload == []:
        content = '[]\n'

    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def list_exporter_targets() -> dict[str, Any]:
    return {
        'target_dir': str(get_target_dir()),
        'process_targets': _flatten_configs(read_target_configs('process')),
        'jmx_targets': _flatten_configs(read_target_configs('jmx')),
        'mysql_targets': _with_tested_database_labels(_flatten_configs(read_target_configs('mysql'))),
        'reload_url': getattr(settings, 'PROMETHEUS_RELOAD_URL', ''),
    }


def _flatten_configs(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for config in configs:
        labels = config.get('labels') or {}
        for target in config.get('targets') or []:
            rows.append({'target': target, 'labels': labels})
    return rows


def _with_tested_database_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = load_tested_database_config()
    host = config.get('host') or ''
    if not host:
        return rows

    port = config.get('port') or 3306
    database = config.get('database') or ''
    db_host = f'{host}:{port}'
    enriched = []
    for row in rows:
        labels = {
            **(row.get('labels') or {}),
            'db_host': db_host,
            'tested_db_host': host,
            'tested_db_port': str(port),
        }
        if database:
            labels.setdefault('database', database)
        enriched.append({**row, 'labels': labels})
    return enriched


def upsert_exporter_target(kind: str, target: str, labels: dict[str, str]) -> None:
    configs = read_target_configs(kind)
    for config in configs:
        targets = config.setdefault('targets', [])
        if target in targets:
            config['labels'] = {**(config.get('labels') or {}), **labels}
            _write_target_configs(kind, configs)
            return

    configs.append({
        'targets': [target],
        'labels': labels,
    })
    _write_target_configs(kind, configs)


def remove_exporter_target(kind: str, target: str) -> bool:
    configs = read_target_configs(kind)
    removed = False
    remaining = []
    for config in configs:
        targets = [item for item in (config.get('targets') or []) if item != target]
        if len(targets) != len(config.get('targets') or []):
            removed = True
        if targets:
            remaining.append({**config, 'targets': targets})
    if removed:
        _write_target_configs(kind, remaining)
    return removed


def recreate_database_exporter() -> dict[str, Any]:
    if not getattr(settings, 'PERFORMANCE_EXPORTER_RESTART_ENABLED', False):
        return {
            'status': 'skipped',
            'message': (
                '数据库 Exporter 自动重建已禁用；请由部署管理员重建服务，或显式设置 '
                'PERFORMANCE_EXPORTER_RESTART_ENABLED=true'
            ),
        }
    compose_file = get_deploy_compose_file()
    if compose_file.exists():
        compose_result = _recreate_database_exporter_with_compose(compose_file)
        if compose_result['status'] == 'success':
            return compose_result

    return _recreate_mysql_exporter_container()


def _recreate_database_exporter_with_compose(compose_file: Path) -> dict[str, Any]:
    command = _compose_command(compose_file)
    try:
        result = subprocess.run(
            command,
            cwd=str(compose_file.parent.parent),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            'status': 'failed',
            'message': str(exc),
            'compose_file': str(compose_file),
        }

    if result.returncode == 0:
        return {
            'status': 'success',
            'message': 'mysql-exporter and prometheus recreated',
            'compose_file': str(compose_file),
        }
    return {
        'status': 'failed',
        'message': (result.stderr or result.stdout or '').strip()[:500] or f'docker compose returned {result.returncode}',
        'compose_file': str(compose_file),
    }


def _compose_command(compose_file: Path) -> list[str]:
    return [
        'docker', 'compose',
        '-f', str(compose_file),
        'up', '-d',
        '--force-recreate',
        'mysql-exporter',
        'prometheus',
    ]


def _recreate_mysql_exporter_container() -> dict[str, Any]:
    config = load_tested_database_config()
    if not config.get('host') or not config.get('user'):
        return {
            'status': 'failed',
            'message': 'APPLY_DB_HOST and APPLY_DB_USER are required for tested database metrics',
        }

    if get_docker_socket_path().exists():
        return _recreate_mysql_exporter_container_with_docker_api(config)

    remove_result = _run_docker_command(['docker', 'rm', '-f', 'runnergo-mysql-exporter'])
    if remove_result['status'] == 'failed' and 'No such container' not in remove_result.get('message', ''):
        return remove_result

    run_command = [
        'docker', 'run', '-d',
        '--name', 'runnergo-mysql-exporter',
        '--restart', 'always',
        '--network', 'runnergo_apipost_net',
        '--network-alias', 'mysql-exporter',
        '-p', '9104:9104',
        '--env', 'MYSQLD_EXPORTER_PASSWORD',
        'prom/mysqld-exporter',
        f"--mysqld.address={config['host']}:{config.get('port') or 3306}",
        f"--mysqld.username={config['user']}",
        '--web.listen-address=:9104',
    ]
    env = {**os.environ, 'MYSQLD_EXPORTER_PASSWORD': config.get('password') or ''}
    run_result = _run_docker_command(run_command, env=env)
    if run_result['status'] == 'success':
        run_result['message'] = 'mysql-exporter recreated'
    return run_result


def _recreate_mysql_exporter_container_with_docker_api(config: dict[str, Any]) -> dict[str, Any]:
    delete_status, delete_payload = _docker_socket_request(
        'DELETE',
        '/containers/runnergo-mysql-exporter?force=true',
    )
    if delete_status not in (204, 404):
        return {
            'status': 'failed',
            'message': delete_payload[:500] or f'Docker delete returned HTTP {delete_status}',
        }

    create_body = {
        'Image': 'prom/mysqld-exporter',
        'Env': [f"MYSQLD_EXPORTER_PASSWORD={config.get('password') or ''}"],
        'Cmd': [
            f"--mysqld.address={config['host']}:{config.get('port') or 3306}",
            f"--mysqld.username={config['user']}",
            '--web.listen-address=:9104',
        ],
        'ExposedPorts': {'9104/tcp': {}},
        'HostConfig': {
            'RestartPolicy': {'Name': 'always'},
            'NetworkMode': 'runnergo_apipost_net',
            'PortBindings': {'9104/tcp': [{'HostPort': '9104'}]},
        },
        'NetworkingConfig': {
            'EndpointsConfig': {
                'runnergo_apipost_net': {
                    'Aliases': ['mysql-exporter', 'runnergo-mysql-exporter'],
                },
            },
        },
    }
    create_status, create_payload = _docker_socket_request(
        'POST',
        '/containers/create?name=runnergo-mysql-exporter',
        create_body,
    )
    if create_status != 201:
        return {
            'status': 'failed',
            'message': create_payload[:500] or f'Docker create returned HTTP {create_status}',
        }

    try:
        container_id = json.loads(create_payload).get('Id')
    except json.JSONDecodeError:
        container_id = ''
    if not container_id:
        return {
            'status': 'failed',
            'message': 'Docker create did not return a container id',
        }

    start_status, start_payload = _docker_socket_request(
        'POST',
        f'/containers/{container_id}/start',
    )
    if start_status not in (204, 304):
        return {
            'status': 'failed',
            'message': start_payload[:500] or f'Docker start returned HTTP {start_status}',
        }

    return {
        'status': 'success',
        'message': 'mysql-exporter recreated',
    }


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: int = 20):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def _docker_socket_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, str]:
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'Content-Type': 'application/json'} if body else {}
    connection = UnixHTTPConnection(get_docker_socket_path())
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode('utf-8', errors='replace')
    finally:
        connection.close()


def _run_docker_command(command: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            'status': 'failed',
            'message': str(exc),
        }

    if result.returncode == 0:
        return {
            'status': 'success',
            'message': (result.stdout or '').strip()[:500] or 'docker command completed',
        }
    return {
        'status': 'failed',
        'message': (result.stderr or result.stdout or '').strip()[:500] or f'docker returned {result.returncode}',
    }


def reload_prometheus(recreate_mysql_exporter: bool = False) -> dict[str, Any]:
    restart = None
    if recreate_mysql_exporter:
        restart = recreate_database_exporter()
        if restart['status'] == 'failed':
            return {
                'status': 'failed',
                'message': restart['message'],
                'database_exporter_restart': restart,
            }

    reload_url = getattr(settings, 'PROMETHEUS_RELOAD_URL', '')
    if not reload_url:
        payload = {
            'status': 'skipped',
            'message': 'PROMETHEUS_RELOAD_URL is not configured',
        }
        if restart:
            payload.update({
                'status': 'success' if restart['status'] == 'success' else 'skipped',
                'message': f"{restart['message']}; Prometheus reload URL is not configured",
                'database_exporter_restart': restart,
            })
        return payload

    try:
        response = requests.post(reload_url, timeout=5)
    except requests.RequestException as exc:
        payload = {
            'status': 'failed',
            'message': str(exc),
        }
        if restart:
            payload['database_exporter_restart'] = restart
        return payload

    if 200 <= response.status_code < 300:
        payload = {
            'status': 'success',
            'message': 'Prometheus reload requested',
        }
        if restart:
            payload['message'] = f"{restart['message']}; Prometheus reload requested"
            payload['database_exporter_restart'] = restart
        return payload
    payload = {
        'status': 'failed',
        'message': f'Prometheus returned HTTP {response.status_code}: {response.text[:200]}',
    }
    if restart:
        payload['database_exporter_restart'] = restart
    return payload
