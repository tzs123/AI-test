"""UI 自动化统一 Webhook 通知发送器。"""
import time
import hmac
import hashlib
import base64
import json
import uuid
from urllib.parse import quote_plus, urlparse
import ipaddress
import requests
from . import settings, db
from .time_utils import localize_task_timestamps
from .url_security import validate_outbound_url


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _record_notification(
    task: dict,
    status: str,
    response: dict = None,
    error: str = "",
    notification_type: str = "feishu",
):
    try:
        db.execute(
            "INSERT INTO notification_logs("
            "id,task_id,job_id,project_id,notification_type,status,title,content,"
            "response_info,error_message,created_at,sent_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:12],
                str(task.get("id") or ""),
                str(task.get("scheduled_job_id") or ""),
                str(task.get("project_id") or ""),
                notification_type,
                status,
                f"UI自动化测试执行{_status_text(task.get('status', ''))}",
                json.dumps({
                    "passed": task.get("passed", 0),
                    "failed": task.get("failed", 0),
                    "total": task.get("total", 0),
                    "report_url": task.get("report_url") or "",
                }, ensure_ascii=False),
                json.dumps(response or {}, ensure_ascii=False),
                error,
                _now(),
                _now() if status in {"success", "failed"} else None,
            ),
        )
    except Exception:
        pass


def _sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _enabled_bots() -> list[dict]:
    url = (
        settings.RUNNERGO_MANAGEMENT_API_URL
        + "/notice/internal/enabled_webhook_bots"
    )
    try:
        response = requests.get(
            url,
            headers={"X-Agent-Token": settings.TEST_DATA_CENTER_AGENT_TOKEN},
            timeout=5,
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json() if response.text else {}
        bots = payload.get("bots") or []
        normalized = [item for item in bots if isinstance(item, dict)]
        if normalized:
            return normalized
    except Exception:
        pass
    return _enabled_bots_from_mysql()


def _enabled_bots_from_mysql() -> list[dict]:
    """Read legacy RunnerGo notice rows when the management bridge is absent."""
    if not settings.RUNNERGO_MYSQL_HOST or not settings.RUNNERGO_MYSQL_PASSWORD:
        return []
    try:
        import pymysql

        connection = pymysql.connect(
            host=settings.RUNNERGO_MYSQL_HOST,
            port=settings.RUNNERGO_MYSQL_PORT,
            user=settings.RUNNERGO_MYSQL_USER,
            password=settings.RUNNERGO_MYSQL_PASSWORD,
            database=settings.RUNNERGO_MYSQL_DATABASE,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
        )
    except Exception:
        return []

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT n.notice_id, n.name, n.params, n.status, n.created_at, "
                "c.type AS channel_type "
                "FROM third_notice n "
                "LEFT JOIN third_notice_channel c ON c.id=n.channel_id "
                "WHERE n.status=1 AND n.deleted_at IS NULL "
                "AND (c.deleted_at IS NULL OR c.id IS NULL)"
            )
            rows = cursor.fetchall() or []
    except Exception:
        return []
    finally:
        try:
            connection.close()
        except Exception:
            pass

    channel_types = {1: "feishu", 2: "wechat", 4: "dingtalk"}
    bots = []
    for row in rows:
        params = row.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (TypeError, ValueError):
                params = {}
        if not isinstance(params, dict) or not params.get("webhook_url"):
            continue
        bot_type = channel_types.get(row.get("channel_type"))
        if not bot_type:
            continue
        bots.append({
            "id": str(row.get("notice_id") or ""),
            "type": bot_type,
            "name": str(row.get("name") or "未命名通知"),
            "webhook_url": str(params.get("webhook_url") or ""),
            "secret": str(params.get("secret") or ""),
            "enabled": True,
            "created_at": str(row.get("created_at") or ""),
        })
    return bots


def notification_targets(bots: list[dict] = None) -> list[dict]:
    """Return browser-safe notification choices without webhook credentials."""
    source = _enabled_bots() if bots is None else bots
    targets = []
    for bot in source:
        notification_id = str(bot.get("id") or "").strip()
        if not notification_id:
            continue
        targets.append({
            "id": notification_id,
            "type": str(bot.get("type") or "webhook").strip().lower(),
            "name": str(bot.get("name") or "未命名通知").strip(),
            "created_at": str(bot.get("created_at") or "").strip(),
        })
    return targets


def select_bots(notification_ids: list[str], bots: list[dict] = None) -> list[dict]:
    """Resolve selected IDs against the latest enabled integrations."""
    wanted = {str(item or "").strip() for item in notification_ids or []}
    wanted.discard("")
    source = _enabled_bots() if bots is None else bots
    return [bot for bot in source if str(bot.get("id") or "").strip() in wanted]


def _color(status: str) -> str:
    if status == "success":
        return "green"
    if status == "failed":
        return "red"
    return "grey"


def _status_text(status: str) -> str:
    return {"success": "✅ 全部通过", "failed": "❌ 存在失败",
            "running": "🏃 执行中", "pending": "⏳ 等待中",
            "stopped": "⛔ 已停止"}.get(status, status)


def _module_text(module: str) -> str:
    return "UI自动化"


def _short_task_id(task_id) -> str:
    return str(task_id or "")[:8]


def _format_duration(duration) -> str:
    if duration in (None, ""):
        return "0s"
    duration_text = str(duration)
    return duration_text if duration_text.endswith("s") else f"{duration_text}s"


def _lark_md(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _is_private_report_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return True
        return ipaddress.ip_address(host).is_private
    except Exception:
        return False


def _task_context(task: dict) -> dict:
    task = localize_task_timestamps(task)
    project_id = task.get("project_id", "")
    project_name = task.get("project_name", "")
    if not project_name and project_id:
        rows = db.execute("SELECT name FROM projects WHERE id=?", (project_id,), fetch=True)
        project_name = rows[0][0] if rows else project_id

    status = task.get("status", "pending")
    passed = int(task.get("passed", 0) or 0)
    failed = int(task.get("failed", 0) or 0)
    skipped = int(task.get("skipped", 0) or 0)
    total = int(task.get("total", 0) or 0)
    return {
        "task": task,
        "status": status,
        "status_text": _status_text(status),
        "success": status == "success",
        "project": project_name or project_id or "-",
        "module": _module_text(task.get("module", "")),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "rate": f"{passed / total * 100:.1f}%" if total else "0%",
        "duration": _format_duration(task.get("duration")),
        "trigger": task.get("triggered_by") or "manual",
        "started_at": task.get("started_at") or "-",
        "finished_at": task.get("finished_at") or "-",
        "report_url": task.get("report_url") or "",
        "title": f"UI自动化测试执行报告 - {_short_task_id(task.get('id'))}",
    }


def _markdown(context: dict) -> str:
    lines = [
        f"**项目：** {context['project']}　**模块：** {context['module']}",
        f"**状态：** {context['status_text']}　**通过率：** {context['rate']}",
        (
            f"**总计：** {context['total']}　**通过：** {context['passed']}　"
            f"**失败：** {context['failed']}　**跳过：** {context['skipped']}"
        ),
        f"**耗时：** {context['duration']}　**触发：** {context['trigger']}",
        f"**开始：** {context['started_at']}　**结束：** {context['finished_at']}",
    ]
    if context["report_url"]:
        lines.append(f"**Allure 报告：** {context['report_url']}")
    return "\n\n".join(lines)


def _feishu_body(context: dict) -> dict:
    elements = [
        _lark_md(f"**项目：** {context['project']}　**模块：** {context['module']}"),
        _lark_md(f"**状态：** {context['status_text']}　**通过率：** {context['rate']}"),
        _lark_md(
            f"**总计：** {context['total']}　**通过：** {context['passed']}　"
            f"**失败：** {context['failed']}　**跳过：** {context['skipped']}"
        ),
        _lark_md(f"**耗时：** {context['duration']}　**触发：** {context['trigger']}"),
        _lark_md(f"**开始：** {context['started_at']}　**结束：** {context['finished_at']}"),
    ]
    report_url = context["report_url"]
    if report_url:
        if _is_private_report_url(report_url):
            elements.append(_lark_md("**报告访问：** 当前为本机地址，请在运行平台的这台机器上打开"))
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看 Allure 报告"},
                "url": report_url,
                "type": "primary",
            }],
        })
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": context["title"]},
                "template": _color(context["status"]),
            },
            "elements": elements,
        },
    }


def _wechat_body(context: dict) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {"content": f"**{context['title']}**\n\n{_markdown(context)}"},
    }


def _dingtalk_body(context: dict) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": context["title"],
            "text": f"**{context['title']}**\n\n{_markdown(context)}",
        },
    }


def _signed_dingtalk_url(webhook_url: str, secret: str) -> str:
    if not secret:
        return webhook_url
    timestamp = str(round(time.time() * 1000))
    signature = quote_plus(base64.b64encode(hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}\n{secret}".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()))
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={timestamp}&sign={signature}"


def send_card_to_bots(task: dict, bots: list[dict]):
    """Send one UI report to an explicit set of third-party integrations."""
    if not bots:
        result = {"skipped": True, "reason": "未选择可用的通知"}
        _record_notification(task, "skipped", result, notification_type="webhook")
        return {"results": [], **result}

    context = _task_context(task)
    results = []
    for bot in bots:
        bot_type = str(bot.get("type") or "").lower()
        webhook_url = str(bot.get("webhook_url") or "").strip()
        if not webhook_url:
            continue
        try:
            webhook_url = validate_outbound_url(webhook_url)
            if bot_type == "feishu":
                body = _feishu_body(context)
            elif bot_type == "wechat":
                body = _wechat_body(context)
            elif bot_type == "dingtalk":
                body = _dingtalk_body(context)
                webhook_url = _signed_dingtalk_url(webhook_url, str(bot.get("secret") or ""))
            else:
                continue

            response = requests.post(
                webhook_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=10,
                allow_redirects=False,
            )
            provider_result = response.json() if response.text else {"status": response.status_code}
            response_ok = 200 <= int(response.status_code) < 300
        except Exception as exc:
            provider_result = {"error": type(exc).__name__}
            response_ok = False

        _record_notification(
            task,
            "success" if response_ok else "failed",
            provider_result,
            "" if response_ok else "Webhook 请求失败",
            notification_type=bot_type or "webhook",
        )
        results.append({
            "id": str(bot.get("id") or ""),
            "type": bot_type,
            "name": bot.get("name") or bot_type,
            "success": response_ok,
            "response": provider_result,
        })
    return {"results": results}


def send_card(task: dict):
    """向 RunnerGo 第三方集成中启用的机器人发送 UI 自动化执行报告。task 至少含:
    id, project_id, module, status, passed, failed, total, duration,
    triggered_by, report_url, started_at, finished_at
    """
    bots = _enabled_bots()
    if not bots:
        result = {"skipped": True, "reason": "RunnerGo 第三方集成中未配置启用的通知机器人"}
        _record_notification(task, "skipped", result, notification_type="webhook")
        return result
    results = send_card_to_bots(task, bots)["results"]
    if len(results) == 1:
        return results[0]["response"]
    return {"results": results}


def send_email(task: dict, emails: list[str] = None):
    """记录邮箱通知意图。当前 UI 自动化服务尚未配置 SMTP，避免误报为已发送。"""
    result = {
        "skipped": True,
        "reason": "email 未配置",
        "emails": emails or [],
    }
    _record_notification(task, "skipped", result, notification_type="email")
    return result


# 兼容旧调用（纯文本）
def send_message(text: str):
    return send_card({
        "id": "message",
        "project_id": "",
        "module": "ui",
        "status": "success",
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "total": 0,
        "duration": 0,
        "triggered_by": "manual",
        "started_at": "-",
        "finished_at": "-",
        "project_name": text,
    })
