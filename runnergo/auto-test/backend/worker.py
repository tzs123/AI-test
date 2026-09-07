"""负载均衡 worker：从 Redis 队列消费任务并执行。

启动多个实例即可实现负载均衡：
    python -m backend.worker
    python -m backend.worker   # 第二个节点
"""
import os
import sys
import time
import signal
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import redis_queue, executor


_RUNNING = True
_HEARTBEAT_INTERVAL = 5


def _sigterm(*_):
    global _RUNNING
    _RUNNING = False


def _heartbeat_loop(name: str, stopped: threading.Event):
    """独立刷新心跳，确保长时间执行用例时仍能被 API 检测到。"""
    while not stopped.is_set():
        try:
            redis_queue.touch_worker(name)
        except Exception:
            pass
        stopped.wait(_HEARTBEAT_INTERVAL)


def worker_loop(name: str = "worker-1"):
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    print(f"🚀 {name} 启动，等待任务...", flush=True)

    heartbeat_stopped = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(name, heartbeat_stopped),
        name=f"{name}-heartbeat",
        daemon=True,
    )
    heartbeat.start()

    try:
        while _RUNNING:
            if not redis_queue.available():
                print("⚠️ Redis 不可用，5s 后重试", flush=True)
                time.sleep(5)
                continue
            task = redis_queue.pop_task(timeout=5)
            if not task:
                continue
            task_id = task.get("task_id")
            print(f"📌 {name} 拉取任务: {task_id}", flush=True)
            try:
                executor.run_task(task_id)
                print(f"✅ {name} 完成任务: {task_id}", flush=True)
            except Exception as e:
                print(f"❌ {name} 任务异常: {task_id} {e}", flush=True)
    finally:
        heartbeat_stopped.set()
        heartbeat.join(timeout=2)
        try:
            redis_queue.remove_worker(name)
        except Exception:
            pass

    print(f"🛑 {name} 已停止", flush=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=f"worker-{os.getpid()}")
    args = p.parse_args()
    worker_loop(args.name)
