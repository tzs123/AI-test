from fnmatch import fnmatch

from backend import redis_queue


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = {"value": value, "ttl": ex}

    def delete(self, key):
        self.values.pop(key, None)

    def scan_iter(self, match=None):
        for key in self.values:
            if match is None or fnmatch(key, match):
                yield key


def test_worker_heartbeat_registration_and_removal(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(redis_queue, "_client", lambda: client)
    monkeypatch.setattr(redis_queue, "_queue", lambda: "test_tasks")

    redis_queue.touch_worker("worker-a")
    redis_queue.touch_worker("worker-b", ttl=30)

    assert redis_queue.worker_count() == 2
    assert client.values["test_tasks:workers:worker-a"]["ttl"] == 15
    assert client.values["test_tasks:workers:worker-b"]["ttl"] == 30

    redis_queue.remove_worker("worker-a")

    assert redis_queue.worker_count() == 1
