"""消息队列协议驱动：Kafka 与 RabbitMQ/AMQP。

YAML 步骤示例::

    - id: produce
      action: kafka
      brokers: "127.0.0.1:9092"
      topic: load-test
      message: hello
      op: produce            # produce / consume
    - id: publish
      action: rabbitmq
      url: amqp://127.0.0.1:5672
      queue: load-test
      routing_key: load-test
      message: hello
      op: publish            # publish / consume
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from backend.url_security import validate_outbound_hostport_url
from .base import (
    attach_runtime_result,
    base_result,
    protocol_sessions,
    register_driver,
    require_dependency,
    resolve,
)
from .base import ProtocolDriver


def _message_text(step: Dict[str, Any], context: Any) -> str:
    message = resolve(context, step.get("message") or step.get("data") or "")
    if isinstance(message, (dict, list)):
        return json.dumps(message, ensure_ascii=False)
    return str(message if message is not None else "")


class KafkaDriver(ProtocolDriver):
    name = "kafka"
    actions = {"kafka"}
    optional_dependency = "kafka-python (pip install kafka-python)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            from kafka import KafkaConsumer, KafkaProducer
        except ImportError:
            require_dependency("kafka-python", "pip install kafka-python")
        brokers = str(resolve(context, step.get("brokers") or "127.0.0.1:9092"))
        topic = str(resolve(context, step.get("topic") or ""))
        op = str(resolve(context, step.get("op") or "produce")).lower()
        result = base_result(step, action="kafka")
        result["url"] = f"kafka://{brokers}/{topic}"
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            if op == "consume":
                consumer = KafkaConsumer(
                    topic,
                    bootstrap_servers=brokers.split(","),
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    consumer_timeout_ms=int(resolve(context, step.get("timeout_ms") or 1000) or 1000),
                    value_deserializer=lambda v: v.decode("utf-8", "replace") if v else "",
                )
                messages = []
                for record in consumer:
                    messages.append(record.value)
                    break
                consumer.close()
                result["text"] = messages[0] if messages else ""
                result["body"] = messages
                result["status"] = "ok"
            else:
                key = ("kafka", brokers)
                producer = pool.get(key)
                if producer is None:
                    producer = KafkaProducer(
                        bootstrap_servers=brokers.split(","),
                        value_serializer=lambda v: v.encode("utf-8"),
                    )
                    pool[key] = producer
                producer.send(topic, _message_text(step, context))
                producer.flush()
                result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


class RabbitMqDriver(ProtocolDriver):
    name = "rabbitmq"
    actions = {"rabbitmq", "amqp"}
    optional_dependency = "pika (pip install pika)"

    def execute(self, step: Dict[str, Any], context: Any) -> Dict[str, Any]:
        try:
            import pika
        except ImportError:
            require_dependency("pika", "pip install pika")
        raw_url = str(resolve(context, step.get("url") or "amqp://127.0.0.1:5672"))
        scheme, host, port, _path, _query = validate_outbound_hostport_url(raw_url)
        queue = str(resolve(context, step.get("queue") or ""))
        exchange = str(resolve(context, step.get("exchange") or ""))
        routing_key = str(resolve(context, step.get("routing_key") or queue))
        op = str(resolve(context, step.get("op") or "publish")).lower()
        result = base_result(step, action="rabbitmq")
        result["url"] = raw_url
        started = time.monotonic()
        try:
            pool = protocol_sessions(context)
            key = ("amqp", raw_url)
            channel = pool.get(key)
            if channel is None:
                params = pika.ConnectionParameters(host=host, port=port or 5672)
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                pool[key] = channel
            if op == "consume":
                method, _props, body = channel.basic_get(queue=queue, auto_ack=True)
                result["text"] = body.decode("utf-8", "replace") if body else ""
                result["body"] = body.hex() if body else ""
                result["status"] = "ok" if method else "empty"
            else:
                if queue and not exchange:
                    channel.queue_declare(queue=queue, durable=False)
                channel.basic_publish(
                    exchange=exchange or "",
                    routing_key=routing_key,
                    body=_message_text(step, context),
                )
                result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["body"] = str(exc)
            attach_runtime_result(exc, result)
            raise
        finally:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result


register_driver(KafkaDriver())
register_driver(RabbitMqDriver())
