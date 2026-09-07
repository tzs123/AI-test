from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings


DB_ENV_KEYS = (
    'APPLY_DB_HOST',
    'APPLY_DB_PORT',
    'APPLY_DB_USER',
    'APPLY_DB_PWD',
    'APPLY_DB_NAME',
)


def get_tested_database_env_path() -> Path:
    configured = getattr(settings, 'TESTED_DATABASE_ENV_FILE', '')
    if configured:
        return Path(configured)
    base_dir = Path(settings.BASE_DIR)
    candidates = [
        base_dir / '.secrets' / 'runnergo.env',
        base_dir.parent / '.secrets' / 'runnergo.env',
    ]
    for candidate in candidates:
        if candidate.exists() or candidate.parent.exists():
            return candidate
    return candidates[-1]


def load_env_values(path: Path | None = None) -> dict[str, str]:
    env_path = path or get_tested_database_env_path()
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key, value = stripped.split('=', 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _unquote_env_value(value.strip())
    return values


def load_tested_database_config() -> dict[str, Any]:
    values = load_env_values()
    for key in DB_ENV_KEYS:
        if os.getenv(key) is not None:
            values[key] = os.getenv(key, '')

    return {
        'host': values.get('APPLY_DB_HOST', '').strip(),
        'port': int(values.get('APPLY_DB_PORT') or 3306),
        'user': values.get('APPLY_DB_USER', '').strip(),
        'password': values.get('APPLY_DB_PWD', ''),
        'database': (
            values.get('APPLY_DB_NAME')
            or values.get('APPLY_DB_DATABASE')
            or values.get('db')
            or ''
        ).strip(),
        'env_file': str(get_tested_database_env_path()),
    }


def redacted_tested_database_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    data = config or load_tested_database_config()
    return {
        'host': data.get('host') or '',
        'port': data.get('port') or 3306,
        'user': data.get('user') or '',
        'database': data.get('database') or '',
        'password_configured': bool(data.get('password')),
        'env_file': data.get('env_file') or str(get_tested_database_env_path()),
    }


def ui_tested_database_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return configuration metadata without ever returning the saved secret."""
    return redacted_tested_database_config(config)


def save_tested_database_config(data: dict[str, Any]) -> dict[str, Any]:
    env_path = get_tested_database_env_path()
    existing = load_env_values(env_path)
    updates = {
        'APPLY_DB_HOST': data.get('host', '').strip(),
        'APPLY_DB_PORT': str(data.get('port') or 3306),
        'APPLY_DB_USER': data.get('user', '').strip(),
        'APPLY_DB_NAME': data.get('database', '').strip(),
    }
    if 'password' in data:
        updates['APPLY_DB_PWD'] = data.get('password') or ''

    merged = {**existing, **updates}
    _write_env_values(env_path, merged)

    for key in DB_ENV_KEYS:
        if key in merged:
            os.environ[key] = merged[key]

    return redacted_tested_database_config(load_tested_database_config())


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    seen = set()

    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                lines.append(line)
                continue
            key = stripped.split('=', 1)[0].strip()
            if key in DB_ENV_KEYS:
                lines.append(f'{key}={_format_env_value(values.get(key, ""))}')
                seen.add(key)
            else:
                lines.append(line)

    for key in DB_ENV_KEYS:
        if key not in seen and key in values:
            lines.append(f'{key}={_format_env_value(values.get(key, ""))}')

    content = '\n'.join(lines).rstrip() + '\n'
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _format_env_value(value: str) -> str:
    text = str(value)
    if not text or any(char.isspace() for char in text) or any(char in text for char in ['#', '"', "'"]):
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return text
