"""Redis 任务队列（负载均衡：多 worker 从同一队列消费）。"""
import json
import time
import redis
from . import settings


WORKER_HEARTBEAT_TTL = 15
LOAD_AGENT_HEARTBEAT_TTL = max(15, int(__import__('os').environ.get('LOAD_AGENT_HEARTBEAT_TTL', '30')))


def _client() -> redis.Redis:
    cfg = settings.REDIS_CFG
    return redis.Redis(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 6379)),
        password=cfg.get("password"),
        db=int(cfg.get("db", 0)),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=30,
    )


def _queue() -> str:
    return settings.REDIS_CFG.get("queue", "test_tasks")


def _worker_key(name: str) -> str:
    return f"{_queue()}:workers:{name}"


def push_task(task: dict):
    _client().lpush(_queue(), json.dumps(task))


def remove_task(task_id: str) -> int:
    """Remove queued copies of a task that has been stopped before workers pick it up."""
    removed = 0
    client = _client()
    queue = _queue()
    for raw in client.lrange(queue, 0, -1):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if str(payload.get("task_id") or "") == str(task_id):
            removed += client.lrem(queue, 0, raw)
    return removed


def pop_task(timeout: int = 5) -> dict:
    """阻塞式弹出任务，便于 worker 长轮询。"""
    try:
        data = _client().brpop(_queue(), timeout=timeout)
    except redis.exceptions.TimeoutError:
        return None
    if data:
        return json.loads(data[1])
    return None


def queue_len() -> int:
    return _client().llen(_queue())


def available() -> bool:
    try:
        _client().ping()
        return True
    except Exception:
        return False


def touch_worker(name: str, ttl: int = WORKER_HEARTBEAT_TTL) -> None:
    """写入带过期时间的 worker 心跳，支持跨容器存活检测。"""
    _client().set(_worker_key(name), str(time.time()), ex=ttl)


def remove_worker(name: str) -> None:
    """worker 正常退出时主动移除心跳；异常退出则由 TTL 自动清理。"""
    _client().delete(_worker_key(name))


def worker_count() -> int:
    """返回当前仍有有效心跳的 worker 数量。"""
    pattern = f"{_queue()}:workers:*"
    return sum(1 for _ in _client().scan_iter(match=pattern))


# ===== Distributed load-generator control plane =====

def _load_agent_prefix() -> str:
    return f"{_queue()}:load_agents"


def _load_agent_key(name: str) -> str:
    return f"{_load_agent_prefix()}:{name}"


def _load_command_key(name: str) -> str:
    return f"{_queue()}:load_commands:{name}"


def _load_payload_key(run_id: str) -> str:
    return f"{_queue()}:load_payload:{run_id}"


def _load_stop_key(run_id: str) -> str:
    return f"{_queue()}:load_stop:{run_id}"


def _load_event_key(run_id: str) -> str:
    return f"{_queue()}:load_events:{run_id}"


def _load_reservation_key(name: str) -> str:
    return f"{_queue()}:load_reservations:{name}"


def touch_load_agent(name: str, capabilities: dict | None = None, ttl: int = LOAD_AGENT_HEARTBEAT_TTL) -> None:
    payload = {
        "name": str(name),
        "role": "load-agent",
        "updated_at": time.time(),
        "capabilities": capabilities or {},
    }
    _client().set(_load_agent_key(str(name)), json.dumps(payload, ensure_ascii=False), ex=ttl)


def remove_load_agent(name: str) -> None:
    _client().delete(_load_agent_key(str(name)))


def list_load_agents() -> list[dict]:
    client = _client()
    result = []
    for key in client.scan_iter(match=f"{_load_agent_prefix()}:*"):
        raw = client.get(key)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("name"):
            reserved_by = client.get(_load_reservation_key(str(payload["name"]))) or ""
            payload["reserved_by"] = reserved_by
            payload["available"] = not bool(reserved_by)
            result.append(payload)
    return sorted(result, key=lambda item: str(item.get("name")))


def reserve_load_agent(name: str, run_id: str, ttl: int) -> bool:
    return bool(_client().set(
        _load_reservation_key(str(name)),
        str(run_id),
        ex=max(60, int(ttl)),
        nx=True,
    ))


def release_load_agent(name: str, run_id: str) -> bool:
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    return bool(_client().eval(script, 1, _load_reservation_key(str(name)), str(run_id)))


def store_load_payload(run_id: str, payload: dict, ttl: int = 900) -> str:
    key = _load_payload_key(run_id)
    _client().set(key, json.dumps(payload, ensure_ascii=False), ex=max(60, int(ttl)))
    return key


def read_load_payload(key: str) -> dict | None:
    raw = _client().get(str(key))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def delete_load_payload(run_id: str) -> None:
    _client().delete(_load_payload_key(run_id))


def push_load_command(agent_name: str, payload: dict) -> None:
    _client().lpush(_load_command_key(agent_name), json.dumps(payload, ensure_ascii=False))


def pop_load_command(agent_name: str, timeout: int = 5) -> dict | None:
    data = _client().brpop(_load_command_key(agent_name), timeout=max(0, int(timeout)))
    if not data:
        return None
    try:
        value = json.loads(data[1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def request_load_stop(run_id: str, ttl: int = 900) -> None:
    _client().set(_load_stop_key(run_id), "1", ex=max(60, int(ttl)))


def load_stop_requested(run_id: str) -> bool:
    return bool(_client().exists(_load_stop_key(run_id)))


def clear_load_stop(run_id: str) -> None:
    _client().delete(_load_stop_key(run_id))


def push_load_event(run_id: str, payload: dict, ttl: int = 900) -> None:
    client = _client()
    key = _load_event_key(run_id)
    client.lpush(key, json.dumps(payload, ensure_ascii=False))
    client.expire(key, max(60, int(ttl)))


def pop_load_event(run_id: str, timeout: int = 1) -> dict | None:
    data = _client().brpop(_load_event_key(run_id), timeout=max(0, int(timeout)))
    if not data:
        return None
    try:
        value = json.loads(data[1])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def clear_load_events(run_id: str) -> None:
    _client().delete(_load_event_key(run_id), _load_payload_key(run_id), _load_stop_key(run_id))
