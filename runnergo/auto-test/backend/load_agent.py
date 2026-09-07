"""Distributed load-generator agent.

The API process is the controller.  Agents deliberately keep no durable case
state: a controller stores the immutable runtime payload in Redis with a TTL,
then sends each agent only its contiguous VU shard.  Samples are returned in
small batches so a high-throughput run does not create one Redis command per
transaction.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import load_test, redis_queue  # noqa: E402


_RUNNING = True
_HEARTBEAT_INTERVAL = max(2.0, float(os.environ.get("LOAD_AGENT_HEARTBEAT_INTERVAL", "5")))
_SAMPLE_BATCH_SIZE = max(1, int(os.environ.get("LOAD_AGENT_SAMPLE_BATCH_SIZE", "50")))
_SAMPLE_FLUSH_SECONDS = max(0.1, float(os.environ.get("LOAD_AGENT_SAMPLE_FLUSH_SECONDS", "0.5")))
_MAX_VUS = max(1, int(os.environ.get("LOAD_AGENT_MAX_VUS", "500")))
_THREAD_STACK_KB = max(0, int(os.environ.get("LOAD_AGENT_THREAD_STACK_KB", "512")))


def _sigterm(*_args: Any) -> None:
    global _RUNNING
    _RUNNING = False


class _SampleBatch:
    def __init__(self, run_id: str, agent_id: str) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._samples: list[dict] = []
        self._last_flush = time.monotonic()

    def add(self, sample: dict) -> None:
        with self._lock:
            item = dict(sample)
            item["agent_id"] = self.agent_id
            self._samples.append(item)
            due = (
                len(self._samples) >= _SAMPLE_BATCH_SIZE
                or time.monotonic() - self._last_flush >= _SAMPLE_FLUSH_SECONDS
            )
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._samples:
                self._last_flush = time.monotonic()
                return
            samples = self._samples
            self._samples = []
            self._last_flush = time.monotonic()
        try:
            redis_queue.push_load_event(
                self.run_id,
                {"kind": "samples", "agent_id": self.agent_id, "samples": samples},
            )
        except Exception:
            # The controller will mark a shard incomplete if Redis is lost.  Do
            # not make the VU thread fail merely because telemetry is unavailable.
            pass


def _heartbeat_loop(agent_id: str, stopped: threading.Event) -> None:
    capabilities = {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "max_vus": _MAX_VUS,
        "modes": ["protocol"],
        "browser": False,
        "version": "load-agent-v1",
    }
    while _RUNNING and not stopped.is_set():
        try:
            redis_queue.touch_load_agent(agent_id, capabilities=capabilities)
        except Exception as exc:
            print(f"[{agent_id}] Redis 心跳失败: {exc}", flush=True)
        stopped.wait(_HEARTBEAT_INTERVAL)


def _wait_until(start_at: float, state: load_test.LoadRunState) -> bool:
    while _RUNNING and not state.stop_event.is_set():
        remaining = float(start_at) - time.time()
        if remaining <= 0:
            return True
        state.stop_event.wait(min(0.25, remaining))
    return False


def _stop_watcher(run_id: str, state: load_test.LoadRunState, stopped: threading.Event) -> None:
    while _RUNNING and not stopped.is_set() and not state.stop_event.is_set():
        try:
            if redis_queue.load_stop_requested(run_id):
                state.status = "stopping"
                state.stop_event.set()
                return
        except Exception:
            # A transient Redis failure must not abort an in-flight request.
            pass
        stopped.wait(0.25)


def _run_shard(agent_id: str, command: dict) -> None:
    run_id = str(command.get("run_id") or "").strip()
    payload_key = str(command.get("payload_key") or "").strip()
    vu_ids = [int(value) for value in (command.get("vu_ids") or [])]
    if not run_id or not payload_key or not vu_ids:
        return
    if len(vu_ids) > _MAX_VUS:
        redis_queue.push_load_event(run_id, {
            "kind": "shard_finished",
            "agent_id": agent_id,
            "status": "failed",
            "error_message": f"分片包含 {len(vu_ids)} VU，超过节点上限 {_MAX_VUS}",
        })
        return

    payload = redis_queue.read_load_payload(payload_key)
    if not isinstance(payload, dict):
        redis_queue.push_load_event(run_id, {
            "kind": "shard_finished",
            "agent_id": agent_id,
            "status": "failed",
            "error_message": "运行时 payload 已过期或不存在",
        })
        return

    config = dict(payload.get("config")) if isinstance(payload.get("config"), dict) else {}
    config["rendezvous_parties"] = len(vu_ids)
    flow = payload.get("flow") if isinstance(payload.get("flow"), dict) else {}
    flows = payload.get("flows") if isinstance(payload.get("flows"), dict) else {}
    state = load_test.LoadRunState(
        run_id=run_id,
        project_id=str(payload.get("project_id") or ""),
        case_file=str(payload.get("case_file") or ""),
        scenario_name=str(payload.get("scenario_name") or payload.get("case_file") or run_id),
        mode=str(config.get("mode") or "protocol"),
        flow=flow,
        config=config,
        flows=flows,
    )
    batch = _SampleBatch(run_id, agent_id)
    state.sample_callback = batch.add
    state.monitor_callback = lambda event: redis_queue.push_load_event(
        run_id,
        {"kind": "monitor", "agent_id": agent_id, "sample": event.get("sample") or {}},
    )
    stopped = threading.Event()
    watcher = threading.Thread(
        target=_stop_watcher,
        args=(run_id, state, stopped),
        daemon=True,
        name=f"{agent_id}-stop-{run_id[:8]}",
    )
    start_at = float(command.get("start_at") or time.time())
    status = "completed"
    error_message = ""
    try:
        if not _wait_until(start_at, state):
            status = "stopped"
            return
        state.started_clock = time.monotonic()
        watcher.start()
        load_test._run_controller(state, vu_ids=vu_ids, persist=False)
        status = state.status
        error_message = state.error_message
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        stopped.set()
        if watcher.is_alive():
            watcher.join(timeout=1)
        batch.flush()
        try:
            redis_queue.push_load_event(run_id, {
                "kind": "shard_finished",
                "agent_id": agent_id,
                "status": status,
                "error_message": str(error_message)[:1000],
                "vu_ids": vu_ids,
                "completed": state.completed,
                "passed": state.passed,
                "failed": state.failed,
            })
        except Exception as exc:
            print(f"[{agent_id}] 回传分片结果失败: {exc}", flush=True)


def load_agent_loop(agent_id: str | None = None) -> None:
    global _RUNNING
    if _THREAD_STACK_KB:
        threading.stack_size(_THREAD_STACK_KB * 1024)
    agent_id = str(agent_id or os.environ.get("LOAD_AGENT_NAME") or f"load-agent-{socket.gethostname()}-{os.getpid()}")
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    print(f"🚀 {agent_id} 启动，等待分布式压测分片...", flush=True)

    heartbeat_stopped = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(agent_id, heartbeat_stopped),
        daemon=True,
        name=f"{agent_id}-heartbeat",
    )
    heartbeat.start()
    active: dict[str, threading.Thread] = {}
    try:
        while _RUNNING:
            for run_id, thread in list(active.items()):
                if not thread.is_alive():
                    active.pop(run_id, None)
            try:
                command = redis_queue.pop_load_command(agent_id, timeout=5)
            except Exception as exc:
                print(f"[{agent_id}] Redis 命令队列不可用: {exc}", flush=True)
                continue
            if not command or str(command.get("kind") or "start") != "start":
                continue
            run_id = str(command.get("run_id") or "").strip()
            if not run_id or run_id in active:
                continue
            thread = threading.Thread(
                target=_run_shard,
                args=(agent_id, command),
                daemon=True,
                name=f"{agent_id}-run-{run_id[:8]}",
            )
            active[run_id] = thread
            thread.start()
    finally:
        for thread in active.values():
            thread.join(timeout=2)
        heartbeat_stopped.set()
        heartbeat.join(timeout=2)
        try:
            redis_queue.remove_load_agent(agent_id)
        except Exception:
            pass
    print(f"🛑 {agent_id} 已停止", flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RunnerGo distributed load agent")
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    load_agent_loop(args.name or None)
